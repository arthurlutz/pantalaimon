#!/usr/bin/env python3

import asyncio
import json
import os
from json import JSONDecodeError
from typing import Any, Dict

import aiohttp
import attr
import keyring
from aiohttp import ClientSession, web
from aiohttp.client_exceptions import (ContentTypeError,
                                       ClientConnectionError)
from multidict import CIMultiDict
from nio import (EncryptionError, LoginResponse, SendRetryError)

from pantalaimon.client import PanClient
from pantalaimon.log import logger
from pantalaimon.store import ClientInfo, PanStore
from pantalaimon.thread_messages import (
    DeviceVerifyMessage,
    DeviceUnverifyMessage,
    ExportKeysMessage,
    ImportKeysMessage,
    DeviceConfirmSasMessage,
    SasMessage,
    AcceptSasMessage,
    DaemonResponse,
)


@attr.s
class ProxyDaemon:
    name = attr.ib()
    homeserver = attr.ib()
    data_dir = attr.ib()
    send_queue = attr.ib()
    recv_queue = attr.ib()
    proxy = attr.ib(default=None)
    ssl = attr.ib(default=None)

    decryption_timeout = 10

    store = attr.ib(type=PanStore, init=False)
    homeserver_url = attr.ib(init=False, default=attr.Factory(dict))
    pan_clients = attr.ib(init=False, default=attr.Factory(dict))
    client_info = attr.ib(
        init=False,
        default=attr.Factory(dict),
        type=dict
    )
    default_session = attr.ib(init=False, default=None)
    database_name = "pan.db"

    def __attrs_post_init__(self):
        self.homeserver_url = self.homeserver.geturl()
        self.hostname = self.homeserver.hostname
        self.store = PanStore(self.data_dir)
        accounts = self.store.load_users(self.hostname)

        self.client_info = self.store.load_clients(self.hostname)

        for user_id, device_id in accounts:
            token = keyring.get_password(
                "pantalaimon",
                f"{user_id}-{device_id}-token"
            )

            if not token:
                logger.warn(f"Not restoring client for {user_id} {device_id}, "
                            f"missing access token.")
                continue

            logger.info(f"Restoring client for {user_id} {device_id}")

            pan_client = PanClient(
                self.homeserver_url,
                self.send_queue,
                user_id,
                device_id,
                store_path=self.data_dir,
                ssl=self.ssl,
                proxy=self.proxy
            )
            pan_client.user_id = user_id
            pan_client.access_token = token
            pan_client.load_store()
            self.pan_clients[user_id] = pan_client

            pan_client.start_loop()

    async def _verify_device(self, message_id, client, device):
        ret = client.verify_device(device)

        if ret:
            msg = (f"Device {device.id} of user "
                   f"{device.user_id} succesfully verified")
        else:
            msg = (f"Device {device.id} of user "
                   f"{device.user_id} already verified")

        logger.info(msg)
        await self.send_response(message_id, client.user_id, "m.ok", msg)

    async def _unverify_device(self, message_id, client, device):
        ret = client.unverify_device(device)

        if ret:
            msg = (f"Device {device.id} of user "
                   f"{device.user_id} succesfully unverified")
        else:
            msg = (f"Device {device.id} of user "
                   f"{device.user_id} already unverified")

        logger.info(msg)
        await self.send_response(message_id, client.user_id, "m.ok", msg)

    async def send_response(self, message_id, pan_user, code, message):
        """Send a thread response message to the UI thread."""
        message = DaemonResponse(message_id, pan_user, code, message)
        await self.send_queue.put(message)

    async def receive_message(self, message):
        client = self.pan_clients.get(message.pan_user)

        if isinstance(
            message,
            (DeviceVerifyMessage, DeviceUnverifyMessage)
        ):

            device = client.device_store[message.user_id].get(
                message.device_id,
                None
            )

            if not device:
                msg = (f"No device found for {message.user_id} and "
                       f"{message.device_id}")
                await self.send_response(
                    message.message_id,
                    message.pan_user,
                    "m.unknown_device",
                    msg
                )
                logger.info(msg)
                return

            if isinstance(message, DeviceVerifyMessage):
                await self._verify_device(message.message_id, client, device)
            elif isinstance(message, DeviceUnverifyMessage):
                await self._unverify_device(message.message_id, client, device)

        elif isinstance(message, SasMessage):
            if isinstance(message, AcceptSasMessage):
                await client.accept_sas(message)
            elif isinstance(message, DeviceConfirmSasMessage):
                await client.confirm_sas(message)

        elif isinstance(message, ExportKeysMessage):
            path = os.path.abspath(os.path.expanduser(message.file_path))
            logger.info(f"Exporting keys to {path}")

            try:
                await client.export_keys(path, message.passphrase)
            except OSError as e:
                info_msg = (f"Error exporting keys for {client.user_id} to"
                            f" {path} {e}")
                logger.info(info_msg)
                await self.send_response(
                    message.message_id,
                    client.user_id,
                    "m.os_error",
                    str(e)
                )

            else:
                info_msg = (f"Succesfully exported keys for {client.user_id} "
                            f"to {path}")
                logger.info(info_msg)
                await self.send_response(
                    message.message_id,
                    client.user_id,
                    "m.ok",
                    info_msg
                )

        elif isinstance(message, ImportKeysMessage):
            path = os.path.abspath(os.path.expanduser(message.file_path))
            logger.info(f"Importing keys from {path}")

            try:
                await client.import_keys(path, message.passphrase)
            except (OSError, EncryptionError) as e:
                info_msg = (f"Error importing keys for {client.user_id} "
                            f"from {path} {e}")
                logger.info(info_msg)
                await self.send_response(
                    message.message_id,
                    client.user_id,
                    "m.os_error",
                    str(e)
                )
            else:
                info_msg = (f"Succesfully imported keys for {client.user_id} "
                            f"from {path}")
                logger.info(info_msg)
                await self.send_response(
                    message.message_id,
                    client.user_id,
                    "m.ok",
                    info_msg
                )

    def get_access_token(self, request):
        # type: (aiohttp.web.BaseRequest) -> str
        """Extract the access token from the request.

        This method extracts the access token either from the query string or
        from the Authorization header of the request.

        Returns the access token if it was found.
        """
        access_token = request.query.get("access_token", "")

        if not access_token:
            access_token = request.headers.get(
                "Authorization",
                ""
            ).strip("Bearer ")

        return access_token

    def sanitize_filter(self, sync_filter):
        # type: (Dict[Any, Any]) -> Dict[Any, Any]
        """Make sure that a filter isn't filtering encrypted messages."""
        sync_filter = dict(sync_filter)
        room_filter = sync_filter.get("room", None)

        if room_filter:
            timeline_filter = room_filter.get("timeline", None)

            if timeline_filter:
                types_filter = timeline_filter.get("types", None)

                if types_filter:
                    if "m.room.encrypted" not in types_filter:
                        types_filter.append("m.room.encrypted")

                not_types_filter = timeline_filter.get("not_types", None)

                if not_types_filter:
                    try:
                        not_types_filter.remove("m.room.encrypted")
                    except ValueError:
                        pass

        return sync_filter

    async def forward_request(
        self,
        request,       # type: aiohttp.web.BaseRequest
        params=None,   # type: CIMultiDict
        data=None,     # type: Dict[Any, Any]
        session=None,  # type: aiohttp.ClientSession
        token=None     # type: str
    ):
        # type: (...) -> aiohttp.ClientResponse
        """Forward the given request to our configured homeserver.

        Args:
            request (aiohttp.BaseRequest): The request that should be
                forwarded.
            params (CIMultiDict, optional): The query parameters for the
                request.
            data (Dict, optional): Data for the request.
            session (aiohttp.ClientSession, optional): The client session that
                should be used to forward the request.
            token (str, optional): The access token that should be used for the
                request.
        """
        if not session:
            if not self.default_session:
                self.default_session = ClientSession()
            session = self.default_session

        assert session

        path = request.path
        method = request.method

        headers = CIMultiDict(request.headers)
        headers.pop("Host", None)

        params = params or CIMultiDict(request.query)

        if token:
            if "Authorization" in headers:
                headers["Authorization"] = f"Bearer {token}"
            if "access_token" in params:
                params["access_token"] = token

        if data:
            data = data
            headers.pop("Content-Length", None)
        else:
            data = await request.read()

        return await session.request(
            method,
            self.homeserver_url + path,
            data=data,
            params=params,
            headers=headers,
            proxy=self.proxy,
            ssl=self.ssl
        )

    async def forward_to_web(
        self,
        request,
        params=None,
        data=None,
        session=None,
        token=None
    ):
        """Forward the given request and convert the response to a Response.

        If there is a exception raised by the client session this method
        returns a Response with a 500 status code and the text set to the error
        message of the exception.

        Args:
            request (aiohttp.BaseRequest): The request that should be
                forwarded.
            params (CIMultiDict, optional): The query parameters for the
                request.
            data (Dict, optional): Data for the request.
            session (aiohttp.ClientSession, optional): The client session that
                should be used to forward the request.
            token (str, optional): The access token that should be used for the
                request.
        """
        try:
            response = await self.forward_request(
                request,
                params=params,
                data=data,
                session=session,
                token=token
            )
            return web.Response(
                status=response.status,
                content_type=response.content_type,
                body=await response.read()
            )
        except ClientConnectionError as e:
            return web.Response(status=500, text=str(e))

    async def router(self, request):
        """Catchall request router."""
        return await self.forward_to_web(request)

    def _get_login_user(self, body):
        identifier = body.get("identifier", None)

        if identifier:
            user = identifier.get("user", None)

            if not user:
                user = body.get("user", "")
        else:
            user = body.get("user", "")

        return user

    async def start_pan_client(self, access_token, user, user_id, password):
        client = ClientInfo(user_id, access_token)
        self.client_info[access_token] = client
        self.store.save_client(self.hostname, client)
        self.store.save_server_user(self.hostname, user_id)

        if user_id in self.pan_clients:
            logger.info(f"Background sync client already exists for {user_id},"
                        f" not starting new one")
            return

        pan_client = PanClient(
            self.homeserver_url,
            self.send_queue,
            user,
            store_path=self.data_dir,
            ssl=self.ssl,
            proxy=self.proxy
        )
        response = await pan_client.login(password, "pantalaimon")

        if not isinstance(response, LoginResponse):
            await pan_client.close()
            return

        logger.info(f"Succesfully started new background sync client for "
                    f"{user_id}")

        self.pan_clients[user_id] = pan_client

        keyring.set_password(
            "pantalaimon",
            f"{user_id}-{pan_client.device_id}-token",
            pan_client.access_token
        )

        pan_client.start_loop()

    async def login(self, request):
        try:
            body = await request.json()
        except (JSONDecodeError, ContentTypeError):
            # After a long debugging session the culprit ended up being aiohttp
            # and a similar bug to
            # https://github.com/aio-libs/aiohttp/issues/2277 but in the server
            # part of aiohttp. The bug is fixed in the latest master of
            # aiohttp.
            # Return 500 here for now since quaternion doesn't work otherwise.
            # After aiohttp 4.0 gets replace this with a 400 M_NOT_JSON
            # response.
            return web.Response(
                status=500,
                text=json.dumps({
                    "errcode": "M_NOT_JSON",
                    "error": "Request did not contain valid JSON."
                })
            )

        user = self._get_login_user(body)
        password = body.get("password", "")

        logger.info(f"New user logging in: {user}")

        try:
            response = await self.forward_request(request)
        except ClientConnectionError as e:
            return web.Response(status=500, text=str(e))

        try:
            json_response = await response.json()
        except (JSONDecodeError, ContentTypeError):
            json_response = None
            pass

        if response.status == 200 and json_response:
            user_id = json_response.get("user_id", None)
            access_token = json_response.get("access_token", None)

            if user_id and access_token:
                logger.info(f"User: {user} succesfully logged in, starting "
                            f"a background sync client.")
                await self.start_pan_client(access_token, user, user_id,
                                            password)

        return web.Response(
            status=response.status,
            content_type=response.content_type,
            body=await response.read()
        )

    @property
    def _missing_token(self):
        return web.Response(
            status=401,
            text=json.dumps({
                "errcode": "M_MISSING_TOKEN",
                "error": "Missing access token."
            })
        )

    @property
    def _unknown_token(self):
        return web.Response(
                status=401,
                text=json.dumps({
                    "errcode": "M_UNKNOWN_TOKEN",
                    "error": "Unrecognised access token."
                })
        )

    @property
    def _not_json(self):
        return web.Response(
            status=400,
            text=json.dumps({
                "errcode": "M_NOT_JSON",
                "error": "Request did not contain valid JSON."
            })
        )

    async def decrypt_body(self, client, body, sync=True):
        """Try to decrypt the a sync or messages body."""
        decryption_method = (
            client.decrypt_sync_body if sync else client.decrypt_messages_body
        )

        async def decrypt_loop(client, body):
            while True:
                try:
                    logger.info("Trying to decrypt sync")
                    return decryption_method(
                        body,
                        ignore_failures=False
                    )
                except EncryptionError:
                    logger.info("Error decrypting sync, waiting for next pan "
                                "sync")
                    await client.synced.wait(),
                    logger.info("Pan synced, retrying decryption.")

        try:
            return await asyncio.wait_for(
                decrypt_loop(client, body),
                timeout=self.decryption_timeout)
        except asyncio.TimeoutError:
            logger.info("Decryption attempt timed out, decrypting with "
                        "failures")
            return decryption_method(body, ignore_failures=True)

    async def sync(self, request):
        access_token = self.get_access_token(request)

        if not access_token:
            return self._missing_token

        try:
            client_info = self.client_info[access_token]
            client = self.pan_clients[client_info.user_id]
        except KeyError:
            return self._unknown_token

        sync_filter = request.query.get("filter", None)
        query = CIMultiDict(request.query)

        if sync_filter:
            try:
                sync_filter = json.loads(sync_filter)
            except (JSONDecodeError, TypeError):
                pass

            if isinstance(sync_filter, dict):
                sync_filter = json.dumps(self.sanitize_filter(sync_filter))

            query["filter"] = sync_filter

        try:
            response = await self.forward_request(
                request,
                params=query,
                token=client.access_token
            )
        except ClientConnectionError as e:
            return web.Response(status=500, text=str(e))

        if response.status == 200:
            try:
                json_response = await response.json()
                json_response = await self.decrypt_body(client, json_response)

                return web.Response(
                    status=response.status,
                    text=json.dumps(json_response)
                )
            except (JSONDecodeError, ContentTypeError):
                pass

        return web.Response(
            status=response.status,
            content_type=response.content_type,
            body=await response.read()
        )

    async def messages(self, request):
        access_token = self.get_access_token(request)

        if not access_token:
            return self._missing_token

        try:
            client_info = self.client_info[access_token]
            client = self.pan_clients[client_info.user_id]
        except KeyError:
            return self._unknown_token

        try:
            response = await self.forward_request(request)
        except ClientConnectionError as e:
            return web.Response(status=500, text=str(e))

        if response.status == 200:
            try:
                json_response = await response.json()
                json_response = await self.decrypt_body(
                    client,
                    json_response,
                    sync=False
                )

                return web.Response(
                    status=response.status,
                    text=json.dumps(json_response)
                )
            except (JSONDecodeError, ContentTypeError):
                pass

        return web.Response(
            status=response.status,
            content_type=response.content_type,
            body=await response.read()
        )

    async def send_message(self, request):
        access_token = self.get_access_token(request)

        if not access_token:
            return self._missing_token

        try:
            client_info = self.client_info[access_token]
            client = self.pan_clients[client_info.user_id]
        except KeyError:
            return self._unknown_token

        room_id = request.match_info["room_id"]

        try:
            encrypt = client.rooms[room_id].encrypted
        except KeyError:
            return await self.forward_to_web(request)

        if not encrypt:
            return await self.forward_to_web(
                request,
                token=client.access_token
            )

        msgtype = request.match_info["event_type"]
        txnid = request.match_info["txnid"]

        try:
            content = await request.json()
        except (JSONDecodeError, ContentTypeError):
            return self._not_json

        try:
            response = await client.room_send(room_id, msgtype, content, txnid)
        except ClientConnectionError as e:
            return web.Response(status=500, text=str(e))
        except SendRetryError as e:
            return web.Response(status=503, text=str(e))

        return web.Response(
            status=response.transport_response.status,
            content_type=response.transport_response.content_type,
            body=await response.transport_response.read()
        )

    async def filter(self, request):
        access_token = self.get_access_token(request)

        if not access_token:
            return self._missing_token

        try:
            content = await request.json()
        except (JSONDecodeError, ContentTypeError):
            return self._not_json

        sanitized_content = self.sanitize_filter(content)

        return await self.forward_to_web(
            request,
            data=json.dumps(sanitized_content)
        )

    async def shutdown(self, app):
        """Shut the daemon down closing all the client sessions it has.

        This method is called when we shut the whole app down.
        """
        for client in self.pan_clients.values():
            await client.loop_stop()
            await client.close()

        if self.default_session:
            await self.default_session.close()
            self.default_session = None
