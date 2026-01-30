#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 1/30/26 1:36 PM
@File       : services/notification.py
@Description: 邮件通知服务 (修复版)
"""
import smtplib
import asyncio
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr  # 🔥 新增
from config.settings import EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER, SMTP_SERVER, SMTP_PORT
from utils.logger import logger


def send_email_sync(subject, content):
    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        return

    try:
        message = MIMEText(content, 'plain', 'utf-8')

        # 🔥 修复核心：生成标准的 "昵称 <邮箱>" 格式
        # 这样 QQ 邮箱就不会报错 550 了
        message['From'] = formataddr(("Solana Bot", EMAIL_SENDER))
        message['To'] = formataddr(("Master", EMAIL_RECEIVER))

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