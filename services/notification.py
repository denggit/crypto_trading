#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : services/notification.py
@Description: 邮件通知服务 (支持附件版)
"""
import smtplib
import os
import asyncio
from email.mime.application import MIMEApplication
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from config.settings import EMAIL_SENDER, EMAIL_RECEIVER, EMAIL_PASSWORD, SMTP_SERVER, SMTP_PORT, BOT_NAME
from utils.logger import logger


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


def _send_email_sync(subject, content, attachment_path=None):
    """ 同步发送邮件逻辑 """
    try:
        msg = MIMEMultipart()
        
        # 🔥 2. 修改这里：自动给标题加上机器人前缀
        # 效果：[激进号] 📊 [日报] 资产与交易总结
        full_subject = f"[{BOT_NAME}] {subject}"
        
        msg["Subject"] = full_subject
        msg["From"] = EMAIL_SENDER
        msg["To"] = EMAIL_RECEIVER

        # 正文
        msg.attach(MIMEText(content, "plain", "utf-8"))

        # 附件
        if attachment_path and os.path.exists(attachment_path):
            with open(attachment_path, "rb") as f:
                part = MIMEApplication(f.read(), Name=os.path.basename(attachment_path))
                part['Content-Disposition'] = f'attachment; filename="{os.path.basename(attachment_path)}"'
                msg.attach(part)

        # 连接 SMTP 服务器发送
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
        
        logger.info(f"📧 邮件发送成功: {full_subject}")
        return True
    except Exception as e:
        logger.error(f"❌ 邮件发送失败: {e}")
        return False
