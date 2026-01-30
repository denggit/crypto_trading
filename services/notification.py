#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 1/30/26 1:20 PM
@File       : notification.py
@Description: 
"""
import asyncio
# services/notification.py
import smtplib
from email.header import Header
from email.mime.text import MIMEText

from config.settings import EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER, SMTP_SERVER, SMTP_PORT
from utils.logger import logger


def send_email_sync(subject, content):
    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        return

    try:
        message = MIMEText(content, 'plain', 'utf-8')
        message['From'] = Header("Solana Bot", 'utf-8')
        message['To'] = Header("Master", 'utf-8')
        message['Subject'] = Header(subject, 'utf-8')

        if "qq.com" in SMTP_SERVER:
            server = smtplib.SMTP_SSL(SMTP_SERVER, 465)
        else:
            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
            server.starttls()

        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, [EMAIL_RECEIVER], message.as_string())
        server.quit()
        logger.info(f"📧 邮件发送成功: {subject}")
    except Exception as e:
        logger.error(f"❌ 邮件发送失败: {e}")


async def send_email_async(subject, content):
    await asyncio.to_thread(send_email_sync, subject, content)
