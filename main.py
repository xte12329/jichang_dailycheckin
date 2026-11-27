#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iKUUU 机场每日签到 - 账号密码自动登录版

每次运行都用账号密码 + GeekedTest 自动过 GeeTest V4 验证码重新登录，
不再依赖会过期的 Cookie，从根本上解决「cookie 每 5 天掉一次」的问题。
同时支持多域名自动轮换，应对 ikuuu 频繁更换域名的情况。

环境变量：
  IKUUU_EMAIL      必填，账号邮箱
  IKUUU_PASSWD     必填，账号密码
  IKUUU_DOMAINS    可选，域名列表(逗号分隔)，默认 ikuuu.top,ikuuu.pw,ikuuu.org
  IKUUU_CAPTCHA_ID 可选，GeeTest V4 captcha_id，默认用 ikuuu 公开值
  SCKEY            可选，Server酱密钥
  PUSHPLUS_TOKEN   可选，PushPlus 密钥
"""
import io
import json
import os
import sys
import time

import requests

# === 强制 UTF-8 输出 ===
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from geeked import Geeked

# === 配置读取 ===
EMAIL = os.environ.get('IKUUU_EMAIL', '').strip()
PASSWD = os.environ.get('IKUUU_PASSWD', '').strip()

# ikuuu 会轮换域名，逐个尝试直到某个可用
DOMAINS_RAW = os.environ.get('IKUUU_DOMAINS', '')
DOMAINS = [d.strip() for d in DOMAINS_RAW.split(',') if d.strip()] if DOMAINS_RAW else [
    'ikuuu.top', 'ikuuu.pw', 'ikuuu.org',
]

# GeeTest V4 captcha_id（ikuuuu 站点公开值；若失效可从登录页 Network 里抓新的）
CAPTCHA_ID = os.environ.get('IKUUU_CAPTCHA_ID', '').strip() or 'cc96d05ba8b60f9112f76e18526fcb73'

# 推送配置
SCKEY = os.environ.get('SCKEY', '')
PUSHPLUS_TOKEN = os.environ.get('PUSHPLUS_TOKEN', '')

MAX_CAPTCHA_RETRY = 3
MAX_DOMAIN_RETRY = 2

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')


def push(title, content):
    """推送通知（Server酱 / PushPlus 二选一）"""
    if SCKEY and SCKEY != '1':
        try:
            url = f'https://sctapi.ftqq.com/{SCKEY}.send'
            requests.post(url, data={'title': title, 'desp': content}, timeout=10)
            print('Server酱推送完成')
        except Exception as e:
            print(f'推送失败: {e}')
    elif PUSHPLUS_TOKEN and PUSHPLUS_TOKEN != '1':
        try:
            data = {'token': PUSHPLUS_TOKEN, 'title': title,
                    'content': content, 'template': 'json'}
            requests.post('http://www.pushplus.plus/send', json=data, timeout=10)
            print('PushPlus 推送完成')
        except Exception as e:
            print(f'推送失败: {e}')
    else:
        print('未配置消息推送')


def solve_captcha():
    """用 GeekedTest 求解 GeeTest V4（ai 无感模式），带重试"""
    for attempt in range(1, MAX_CAPTCHA_RETRY + 1):
        try:
            print(f'[验证码] 求解尝试 {attempt}/{MAX_CAPTCHA_RETRY} ...')
            geeked = Geeked(CAPTCHA_ID, 'ai')
            result = geeked.solve()
            print('[验证码] 求解成功')
            return result
        except Exception as ex:
            print(f'[验证码] 求解失败 ({attempt}): {ex}')
            if attempt < MAX_CAPTCHA_RETRY:
                time.sleep(2)
    return None


def try_domain(domain):
    """在单个域名上尝试登录 + 签到，返回 (是否成功, 消息)"""
    url = f'https://{domain}'
    login_url = f'{url}/auth/login'
    check_url = f'{url}/user/checkin'
    header = {
        'origin': url,
        'referer': f'{url}/auth/login',
        'user-agent': UA,
        'accept': 'application/json, text/javascript, */*; q=0.01',
        'x-requested-with': 'XMLHttpRequest',
    }

    session = requests.Session()

    # 1. 过验证码
    captcha = solve_captcha()
    if not captcha:
        return False, '验证码求解失败'

    # 2. 登录
    data = {
        'email': EMAIL,
        'passwd': PASSWD,
        'captcha_result[lot_number]': captcha['lot_number'],
        'captcha_result[captcha_output]': captcha['captcha_output'],
        'captcha_result[pass_token]': captcha['pass_token'],
        'captcha_result[gen_time]': captcha['gen_time'],
    }
    try:
        resp = session.post(login_url, headers=header, data=data, timeout=20)
        result = resp.json()
    except Exception as ex:
        return False, f'登录请求异常: {ex}'

    if result.get('ret') != 1:
        return False, f'登录失败: {result.get("msg", result)}'

    # 3. 签到
    try:
        resp2 = session.post(check_url, headers=header, timeout=20)
        result2 = resp2.json()
    except Exception as ex:
        return False, f'签到请求异常: {ex}'

    msg = result2.get('msg', str(result2))
    ret = result2.get('ret', None)
    if ret == 1 or ret == 0 or '成功' in msg or '重复' in msg:
        return True, msg
    return False, f'签到返回异常: {msg} (ret={ret})'


def main():
    print('=== iKUUU 机场每日签到（账号密码自动登录版）===')
    if not EMAIL or not PASSWD:
        print('[FATAL] 未设置 IKUUU_EMAIL / IKUUU_PASSWD')
        push('❌ ikuuu 签到失败', '未配置账号密码，请检查 Secrets')
        return False

    print(f'账号: {EMAIL}')
    print(f'候选域名: {", ".join(DOMAINS)}')

    for domain in DOMAINS:
        print(f'\n--- 尝试域名 {domain} ---')
        for attempt in range(1, MAX_DOMAIN_RETRY + 1):
            ok, msg = try_domain(domain)
            if ok:
                print(f'✅ 签到成功 [{domain}]: {msg}')
                push('✅ ikuuu 签到成功', f'域名: {domain}\n{msg}\n邮箱: {EMAIL}')
                return True
            print(f'[{domain}] 第 {attempt} 次失败: {msg}')
            if attempt < MAX_DOMAIN_RETRY:
                time.sleep(3)

    print('[ERROR] 所有域名均签到失败')
    push('❌ ikuuu 签到失败',
         f'所有域名尝试完毕仍失败。\n账号: {EMAIL}\n'
         f'请检查密码是否正确、captcha_id 是否过期，或到 Actions 看详细日志。')
    return False


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
