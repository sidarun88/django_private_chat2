from channels.generic.websocket import AsyncWebsocketConsumer
from channels.layers import InMemoryChannelLayer
from channels.db import database_sync_to_async
from .models import MessageModel, DialogsModel, UserModel, UploadedFile
from .serializers import serialize_file_model
from typing import List, Set, Awaitable, Optional, Dict, Tuple
from django.contrib.auth.models import AbstractBaseUser
from django.conf import settings
from django.core.exceptions import ValidationError
import logging
import json
import enum
import sys

try:
    from typing import TypedDict
except ImportError:
    TypedDict = dict

logger = logging.getLogger('django_private_chat2.consumers')
TEXT_MAX_LENGTH = getattr(settings, 'TEXT_MAX_LENGTH', 65535)


class ErrorTypes(enum.IntEnum):
    MessageParsingError = 1
    TextMessageInvalid = 2
    InvalidMessageReadId = 3
    InvalidUserPk = 4
    InvalidRandomId = 5
    FileMessageInvalid = 6
    FileDoesNotExist = 7


ErrorDescription = Tuple[ErrorTypes, str]


# TODO: add tx_id to distinguish errors for different transactions

class MessageTypeTextMessage(TypedDict):
    text: str
    user_pk: str
    random_id: int


class MessageTypeMessageRead(TypedDict):
    user_pk: str
    message_id: str


class MessageTypeFileMessage(TypedDict):
    file_id: str
    user_pk: str
    random_id: int


class MessageTypes(enum.IntEnum):
    WentOnline = 1
    WentOffline = 2
    TextMessage = 3
    FileMessage = 4
    IsTyping = 5
    MessageRead = 6
    ErrorOccurred = 7
    MessageIdCreated = 8
    NewUnreadCount = 9
    TypingStopped = 10
    Heartbeat = 11


@database_sync_to_async
def get_groups_to_add(u: AbstractBaseUser) -> Awaitable[Set[int]]:
    l = DialogsModel.get_dialogs_for_user(u)
    return set(list(sum(l, ())))


@database_sync_to_async
def get_user_by_pk(pk: str) -> Awaitable[Optional[AbstractBaseUser]]:
    return UserModel.objects.filter(pk=pk).first()


@database_sync_to_async
def get_user_by_username(username: str) -> Awaitable[Optional[AbstractBaseUser]]:
    return UserModel.objects.filter(username=username).first()


@database_sync_to_async
def get_file_by_id(file_id: str) -> Awaitable[Optional[UploadedFile]]:
    try:
        f = UploadedFile.objects.filter(id=file_id).first()
    except ValidationError:
        f = None
    return f


@database_sync_to_async
def get_message_by_id(mid: str) -> Awaitable[Optional[Tuple[str, str]]]:
    msg: Optional[MessageModel] = MessageModel.objects.filter(pid=mid).first()
    if msg:
        return str(msg.recipient.pk), str(msg.sender.pk)
    else:
        return None


# @database_sync_to_async
# def mark_message_as_read(mid: int, sender_pk: str, recipient_pk: str):
#     return MessageModel.objects.filter(id__lte=mid,sender_id=sender_pk, recipient_id=recipient_pk).update(read=True)

@database_sync_to_async
def mark_message_as_read(mid: str) -> Awaitable[None]:
    return MessageModel.objects.filter(pid=mid).update(read=True)


@database_sync_to_async
def get_unread_count(sender, recipient) -> Awaitable[int]:
    return int(MessageModel.get_unread_count_for_dialog_with_user(sender, recipient))


@database_sync_to_async
def save_text_message(text: str, from_: AbstractBaseUser, to: AbstractBaseUser, rid: int, **kwargs) -> Awaitable[MessageModel]:
    return MessageModel.objects.create(text=text, sender=from_, recipient=to, random_id=rid, **kwargs)


@database_sync_to_async
def save_file_message(file: UploadedFile, from_: AbstractBaseUser, to: AbstractBaseUser) -> Awaitable[MessageModel]:
    return MessageModel.objects.create(file=file, sender=from_, recipient=to)


def event_extra_metadata(event, excluded_keys) -> dict:
    return {k: v for k, v in event.items() if k not in excluded_keys}


class ChatConsumer(AsyncWebsocketConsumer):
    async def _after_message_save(self, msg: MessageModel, rid: int, user_pk: str):
        ev = {"type": "message_id_created", "random_id": rid, "db_id": str(msg.pid)}
        logger.info(f"Message with id {msg.id} saved, firing events to {user_pk} & {self.group_name}")
        await self.channel_layer.group_send(user_pk, ev)
        await self.channel_layer.group_send(self.group_name, ev)
        new_unreads = await get_unread_count(self.group_name, user_pk)
        await self.channel_layer.group_send(user_pk,
                                            {"type": "new_unread_count", "sender": self.sender_username,
                                             "unread_count": new_unreads})

    async def connect(self):
        # TODO:
        # 1. Set user online
        # 2. Notify other users that the user went online
        # 3. Add the user to all groups where he has dialogs
        # Call self.scope["session"].save() on any changes to User
        if self.scope["user"].is_authenticated:
            self.user: AbstractBaseUser = self.scope['user']
            self.group_name: str = str(self.user.pk)
            self.sender_username: str = self.user.get_username()
            logger.info(f"User {self.user.pk} connected, adding {self.channel_name} to {self.group_name}")
            await self.channel_layer.group_add(self.group_name, self.channel_name)
            await self.accept()
            dialogs = await get_groups_to_add(self.user)
            logger.info(f"User {self.user.pk} connected, sending 'user_went_online' to {dialogs} dialog groups")
            for d in dialogs:  # type: int
                if str(d) != self.group_name:
                    await self.channel_layer.group_send(str(d),
                                                        {"type": "user_went_online", "user_pk": str(self.user.get_username())})
        else:
            await self.close(code=4001)

    async def disconnect(self, close_code):
        # TODO:
        # Set user offline
        # Save user was_online
        # Notify other users that the user went offline
        if close_code != 4001 and getattr(self, 'user', None) is not None:
            logger.info(
                f"User {self.user.pk} disconnected, removing channel {self.channel_name} from group {self.group_name}")
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
            dialogs = await get_groups_to_add(self.user)
            logger.info(f"User {self.user.pk} disconnected, sending 'user_went_offline' to {dialogs} dialog groups")
            for d in dialogs:
                await self.channel_layer.group_send(str(d), {"type": "user_went_offline", "user_pk": str(self.user.get_username())})

    async def handle_received_message(self, msg_type: MessageTypes, data: Dict[str, str]) -> Optional[ErrorDescription]:
        logger.info(f"Received message type {msg_type.name} from user {self.group_name} with data {data}")
        if msg_type == MessageTypes.WentOffline \
            or msg_type == MessageTypes.WentOnline \
            or msg_type == MessageTypes.MessageIdCreated \
            or msg_type == MessageTypes.ErrorOccurred:
            logger.info(f"Ignoring message {msg_type.name}")
        else:
            if msg_type == MessageTypes.IsTyping:
                if 'user_pk' in data and not isinstance(data['user_pk'], str):
                    return ErrorTypes.MessageParsingError, "'user_pk' should be a string"
                elif 'user_pk' in data:
                    user_pk = data['user_pk']
                    recipient: Optional[AbstractBaseUser] = await get_user_by_username(user_pk)
                    logger.info(f"DB check if user {user_pk} exists resulted in {recipient}")
                    if not recipient:
                        return ErrorTypes.InvalidUserPk, f"User with username {user_pk} does not exist"
                    else:
                        logger.info(f"User {self.user.pk} is typing to {recipient.pk}, sending 'is_typing' to {recipient}")
                        await self.channel_layer.group_send(str(recipient.pk), {"type": "is_typing",
                                                                                "user_pk": str(self.sender_username)})
                else:
                    dialogs = await get_groups_to_add(self.user)
                    logger.info(f"User {self.user.pk} is typing, sending 'is_typing' to {dialogs} dialog groups")
                    for d in dialogs:
                        if str(d) != self.group_name:
                            await self.channel_layer.group_send(str(d), {"type": "is_typing",
                                                                         "user_pk": str(self.sender_username)})
                return None
            elif msg_type == MessageTypes.TypingStopped:
                if 'user_pk' in data and not isinstance(data['user_pk'], str):
                    return ErrorTypes.MessageParsingError, "'user_pk' should be a string"
                elif 'user_pk' in data:
                    user_pk = data['user_pk']
                    recipient: Optional[AbstractBaseUser] = await get_user_by_username(user_pk)
                    logger.info(f"DB check if user {user_pk} exists resulted in {recipient}")
                    if not recipient:
                        return ErrorTypes.InvalidUserPk, f"User with username {user_pk} does not exist"
                    else:
                        logger.info(f"User {self.user.pk} hast stopped typing to {recipient.pk}, sending 'stopped_typing' to {recipient}")
                        await self.channel_layer.group_send(str(recipient.pk), {"type": "stopped_typing",
                                                                                "user_pk": str(self.sender_username)})
                else:
                    dialogs = await get_groups_to_add(self.user)
                    logger.info(f"User {self.user.pk} has stopped typing, sending 'stopped_typing' to {dialogs} dialog groups")
                    for d in dialogs:
                        if str(d) != self.group_name:
                            await self.channel_layer.group_send(str(d), {"type": "stopped_typing",
                                                                         "user_pk": str(self.sender_username)})
                return None
            elif msg_type == MessageTypes.Heartbeat:
                response = await self.heartbeat_received(sender=self.user, data=data)
                return response
            elif msg_type == MessageTypes.MessageRead:
                data: MessageTypeMessageRead
                if 'user_pk' not in data:
                    return ErrorTypes.MessageParsingError, "'user_pk' not present in data"
                elif 'message_id' not in data:
                    return ErrorTypes.MessageParsingError, "'message_id' not present in data"
                elif not isinstance(data['user_pk'], str):
                    return ErrorTypes.InvalidUserPk, "'user_pk' should be a string"
                elif not isinstance(data['message_id'], str):
                    return ErrorTypes.InvalidRandomId, "'message_id' should be an str"
                elif data['user_pk'] == self.sender_username:
                    return ErrorTypes.InvalidUserPk, "'user_pk' can't be self  (you can't mark self messages as read)"
                else:
                    user_pk = data['user_pk']
                    mid = data['message_id']
                    recipient: Optional[AbstractBaseUser] = await get_user_by_username(user_pk)
                    logger.info(f"DB check if user {user_pk} exists resulted in {recipient}")
                    if not recipient:
                        return ErrorTypes.InvalidUserPk, f"User with username {user_pk} does not exist"
                    else:
                        logger.info(
                            f"Validation passed, marking msg from {recipient.pk} to {self.group_name} with pid {mid} as read")
                        await self.channel_layer.group_send(str(recipient.pk), {"type": "message_read",
                                                                                "message_id": mid,
                                                                                "sender": user_pk,
                                                                                "receiver": self.sender_username})
                        msg_res: Optional[Tuple[str, str]] = await get_message_by_id(mid)
                        if not msg_res:
                            return ErrorTypes.InvalidMessageReadId, f"Message with pid {mid} does not exist"
                        elif msg_res[0] != self.group_name or msg_res[1] != str(recipient.pk):
                            return ErrorTypes.InvalidMessageReadId, f"Message with pid {mid} was not sent by {recipient.pk} to {self.group_name}"
                        else:
                            await mark_message_as_read(mid)
                            new_unreads = await get_unread_count(str(recipient.pk), self.group_name)
                            await self.channel_layer.group_send(self.group_name,
                                                                {"type": "new_unread_count", "sender": user_pk,
                                                                 "unread_count": new_unreads})
                            # await mark_message_as_read(mid, sender_pk=user_pk, recipient_pk=self.group_name)

                return None
            elif msg_type == MessageTypes.FileMessage:
                data: MessageTypeFileMessage
                if 'file_id' not in data:
                    return ErrorTypes.MessageParsingError, "'file_id' not present in data"
                elif 'user_pk' not in data:
                    return ErrorTypes.MessageParsingError, "'user_pk' not present in data"
                elif 'random_id' not in data:
                    return ErrorTypes.MessageParsingError, "'random_id' not present in data"
                elif data['file_id'] == '':
                    return ErrorTypes.FileMessageInvalid, "'file_id' should not be blank"
                elif not isinstance(data['file_id'], str):
                    return ErrorTypes.FileMessageInvalid, "'file_id' should be a string"
                elif not isinstance(data['user_pk'], str):
                    return ErrorTypes.InvalidUserPk, "'user_pk' should be a string"
                elif not isinstance(data['random_id'], int):
                    return ErrorTypes.InvalidRandomId, "'random_id' should be an int"
                elif data['random_id'] > 0:
                    return ErrorTypes.InvalidRandomId, "'random_id' should be negative"
                else:
                    file_id = data['file_id']
                    user_pk = data['user_pk']
                    rid = data['random_id']
                    # We can't send the message right away like in the case with text message
                    # because we don't have the file url.
                    file: Optional[UploadedFile] = await get_file_by_id(file_id)
                    logger.info(f"DB check if file {file_id} exists resulted in {file}")
                    if not file:
                        return ErrorTypes.FileDoesNotExist, f"File with id {file_id} does not exist"
                    else:
                        recipient: Optional[AbstractBaseUser] = await get_user_by_username(user_pk)
                        logger.info(f"DB check if user {user_pk} exists resulted in {recipient}")
                        if not recipient:
                            return ErrorTypes.InvalidUserPk, f"User with username {user_pk} does not exist"
                        else:
                            logger.info(f"Will save file message from {self.user} to {recipient}")
                            msg = await save_file_message(file, from_=self.user, to=recipient)
                            await self._after_message_save(msg, rid=rid, user_pk=str(recipient.pk))
                            logger.info(f"Sending file message for file {file_id} from {self.user} to {recipient}")
                            # We don't need to send random_id here because we've already saved the file to db
                            file_message_event = {
                                "type": "new_file_message",
                                "db_id": str(msg.pid),
                                "file": serialize_file_model(file),
                                "sender": self.sender_username,
                                "receiver": user_pk,
                                "sender_channel_name": self.channel_name,
                                **self.sender_metadata(sender=self.user)
                            }
                            await self.channel_layer.group_send(str(recipient.pk), file_message_event)
                            await self.channel_layer.group_send(self.group_name, file_message_event)

            elif msg_type == MessageTypes.TextMessage:
                data: MessageTypeTextMessage
                if 'text' not in data:
                    return ErrorTypes.MessageParsingError, "'text' not present in data"
                elif 'user_pk' not in data:
                    return ErrorTypes.MessageParsingError, "'user_pk' not present in data"
                elif 'random_id' not in data:
                    return ErrorTypes.MessageParsingError, "'random_id' not present in data"
                elif data['text'] == '':
                    return ErrorTypes.TextMessageInvalid, "'text' should not be blank"
                elif len(data['text']) > TEXT_MAX_LENGTH:
                    return ErrorTypes.TextMessageInvalid, "'text' is too long"
                elif not isinstance(data['text'], str):
                    return ErrorTypes.TextMessageInvalid, "'text' should be a string"
                elif not isinstance(data['user_pk'], str):
                    return ErrorTypes.InvalidUserPk, "'user_pk' should be a string"
                elif not isinstance(data['random_id'], int):
                    return ErrorTypes.InvalidRandomId, "'random_id' should be an int"
                elif data['random_id'] > 0:
                    return ErrorTypes.InvalidRandomId, "'random_id' should be negative"
                else:
                    text = data['text']
                    user_pk = data['user_pk']
                    rid = data['random_id']
                    # first we send data to channel layer to not perform any synchronous operations,
                    # and only after we do sync DB stuff
                    recipient: Optional[AbstractBaseUser] = await get_user_by_username(user_pk)
                    logger.info(f"DB check if user {user_pk} exists resulted in {recipient}")
                    if not recipient:
                        return ErrorTypes.InvalidUserPk, f"User with username {user_pk} does not exist"
                    else:
                        # We need to create a 'random id' - a temporary id for the message, which is not yet
                        # saved to the database. I.e. for the client it is 'pending delivery' and can be
                        # considered delivered only when it's saved to database and received a proper id,
                        # which is then broadcast separately both to sender & receiver.
                        logger.info(f"Validation passed, sending text message from {self.group_name} to {recipient.pk}")
                        preview_data = {key: value for key, value in data.items() if "preview" in key}
                        text_message_event = {
                            "type": "new_text_message",
                            "random_id": rid,
                            "text": text,
                            "sender": self.sender_username,
                            "receiver": user_pk,
                            "sender_channel_name": self.channel_name,
                            **preview_data,
                            **self.sender_metadata(sender=self.user)
                        }
                        await self.channel_layer.group_send(str(recipient.pk), text_message_event)
                        await self.channel_layer.group_send(self.group_name, text_message_event)
                        logger.info(f"Will save text message from {self.user} to {recipient}")
                        msg = await save_text_message(text, from_=self.user, to=recipient, rid=rid, **preview_data)
                        await self._after_message_save(msg, rid=rid, user_pk=str(recipient.pk))

    # Receive message from WebSocket
    async def receive(self, text_data=None, bytes_data=None):
        logger.info(f"Receive fired")
        error: Optional[ErrorDescription] = None
        try:
            text_data_json = json.loads(text_data)
            logger.info(f"From {self.group_name} received '{text_data_json}")
            if not ('msg_type' in text_data_json):
                error = (ErrorTypes.MessageParsingError, "msg_type not present in json")
            else:
                msg_type = text_data_json['msg_type']
                if not isinstance(msg_type, int):
                    error = (ErrorTypes.MessageParsingError, "msg_type is not an int")
                else:
                    try:
                        msg_type_case: MessageTypes = MessageTypes(msg_type)
                        error = await self.handle_received_message(msg_type_case, text_data_json)
                    except ValueError as e:
                        error = (ErrorTypes.MessageParsingError, f"msg_type decoding error - {e}")
        except json.JSONDecodeError as e:
            error = (ErrorTypes.MessageParsingError, f"jsonDecodeError - {e}")
        if error is not None:
            error_data = {
                'msg_type': MessageTypes.ErrorOccurred,
                'error': error
            }
            logger.info(f"Will send error {error_data} to {self.group_name}")
            await self.send(text_data=json.dumps(error_data))

    async def new_unread_count(self, event):
        excluded_keys = ('msg_type', 'sender', 'unread_count', 'type')
        await self.send(
            text_data=json.dumps({
                'msg_type': MessageTypes.NewUnreadCount,
                'sender': event['sender'],
                'unread_count': event['unread_count'],
                **event_extra_metadata(event, excluded_keys),
            }))

    async def message_read(self, event):
        excluded_keys = ('msg_type', 'message_id', 'sender', 'receiver', 'type')
        await self.send(
            text_data=json.dumps({
                'msg_type': MessageTypes.MessageRead,
                'message_id': event['message_id'],
                'sender': event['sender'],
                'receiver': event['receiver'],
                **event_extra_metadata(event, excluded_keys),
            }))

    async def message_id_created(self, event):
        excluded_keys = ('msg_type', 'random_id', 'db_id', 'type')
        await self.send(
            text_data=json.dumps({
                'msg_type': MessageTypes.MessageIdCreated,
                'random_id': event['random_id'],
                'db_id': event['db_id'],
                **event_extra_metadata(event, excluded_keys),
            }))

    async def new_text_message(self, event):
        if self.channel_name != event['sender_channel_name']:
            excluded_keys = ('msg_type', 'random_id', 'text', 'sender', 'receiver', 'type', 'sender_channel_name')
            await self.send(
                text_data=json.dumps({
                    'msg_type': MessageTypes.TextMessage,
                    "random_id": event['random_id'],
                    "text": event['text'],
                    "sender": event['sender'],
                    "receiver": event['receiver'],
                    **event_extra_metadata(event, excluded_keys),
                })
            )

    async def new_file_message(self, event):
        if self.channel_name != event['sender_channel_name']:
            excluded_keys = ('msg_type', 'db_id', 'file', 'sender', 'receiver', 'type', 'sender_channel_name')
            await self.send(
                text_data=json.dumps({
                    'msg_type': MessageTypes.FileMessage,
                    "db_id": event['db_id'],
                    "file": event['file'],
                    "sender": event['sender'],
                    "receiver": event['receiver'],
                    **event_extra_metadata(event, excluded_keys),
                })
            )

    async def is_typing(self, event):
        excluded_keys = ('msg_type', 'user_pk', 'type')
        await self.send(
            text_data=json.dumps({
                'msg_type': MessageTypes.IsTyping,
                'user_pk': event['user_pk'],
                **event_extra_metadata(event, excluded_keys),
            }))

    async def stopped_typing(self, event):
        excluded_keys = ('msg_type', 'user_pk', 'type')
        await self.send(
            text_data=json.dumps({
                'msg_type': MessageTypes.TypingStopped,
                'user_pk': event['user_pk'],
                **event_extra_metadata(event, excluded_keys),
            }))

    async def user_went_online(self, event):
        excluded_keys = ('msg_type', 'user_pk', 'type')
        await self.send(
            text_data=json.dumps({
                'msg_type': MessageTypes.WentOnline,
                'user_pk': event['user_pk'],
                **event_extra_metadata(event, excluded_keys),
            }))

    async def user_went_offline(self, event):
        excluded_keys = ('msg_type', 'user_pk', 'type')
        await self.send(
            text_data=json.dumps({
                'msg_type': MessageTypes.WentOffline,
                'user_pk': event['user_pk'],
                **event_extra_metadata(event, excluded_keys),
            }))

    def sender_metadata(self, sender: AbstractBaseUser) -> dict:
        """
        Returns sender's extra data as dict to be included along with messages
        """
        raise NotImplementedError('subclasses of ChatConsumer must provide a sender_metadata() method')

    async def heartbeat_received(self, sender: AbstractBaseUser, data: Dict[str, str]) -> Optional[ErrorDescription]:
        """
        Logic to update user's online status goes here
        """
        raise NotImplementedError('subclasses of ChatConsumer must provide a heartbeat_received() method')
