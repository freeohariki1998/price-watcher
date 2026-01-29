# line_utils.py

from linebot.v3.messaging import (
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer
)

class LineBotHelper:
    def __init__(self, config):
        self.config = config

    def push_text(self, to_id, text):
        with ApiClient(self.config) as client:
            MessagingApi(client).push_message(
                PushMessageRequest(to=to_id, messages=[TextMessage(text=text)])
            )

    def reply_text(self, token, text):
        with ApiClient(self.config) as client:
            MessagingApi(client).reply_message(
                ReplyMessageRequest(reply_token=token, messages=[TextMessage(text=text)])
            )

    def reply_flex(self, token, alt_text, bubble_dict):
        with ApiClient(self.config) as api_client:
            api = MessagingApi(api_client)
            api.reply_message(
                ReplyMessageRequest(
                    reply_token=token,
                    messages=[
                        FlexMessage(
                            alt_text=alt_text,
                            contents=FlexContainer.from_dict(bubble_dict)
                        )
                    ]
                )
            )