#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : services/notification.py
@Description: 邮件通知服务 (支持附件版)
"""
import smtplib
import os
import asyncio
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from config.settings import EMAIL_SENDER, EMAIL_RECEIVER, EMAIL_PASSWORD, SMTP_SERVER, SMTP_PORT


async def send_email_async(subject, content, attachment_path=None):
    """
    发送邮件 (异步封装)
    :param subject: 邮件标题
    :param content: 邮件正文
    :param attachment_path: 附件文件的绝对路径 (可选)
    """
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _send_email_sync, subject, content, attachment_path)
    except Exception as e:
        print(f"❌ 邮件发送后台报错: {e}")


def _send_email_sync(subject, content, attachment_path):
    """ 同步发送逻辑 (由 send_email_async 调用) """
    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        return

    try:
        # 1. 创建复合邮件对象
        msg = MIMEMultipart()
        msg['From'] = EMAIL_SENDER
        msg['To'] = EMAIL_RECEIVER  # 发给自己
        msg['Subject'] = subject

        # 2. 添加正文
        msg.attach(MIMEText(content, 'plain', 'utf-8'))

        # 3. 添加附件 (如果有，且文件存在)
        if attachment_path and os.path.exists(attachment_path):
            filename = os.path.basename(attachment_path)
            with open(attachment_path, "rb") as attachment:
                # 构造附件对象
                part = MIMEBase("application", "octet-stream")
                part.set_payload(attachment.read())

            # 编码并添加头信息
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f"attachment; filename= {filename}",
            )
            msg.attach(part)
            print(f"📎 已添加附件: {filename}")

        # 4. 连接服务器发送
        server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT)
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()

    except Exception as e:
        print(f"❌ 邮件发送失败: {e}")
        raise e