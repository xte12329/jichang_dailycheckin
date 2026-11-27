#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iKUUU 机场每日签到 - 浏览器自动登录版

原「账号密码 + GeekedTest 协议求解」方案已废弃：ikuuu 现在随机返回
九宫格(nine) / 图标(icon) / 文字(word) 三种 GeeTest V4 验证码，
协议求解器只支持其中一部分，签名会被极验以 -50000 拒绝。

本版改用真实 Chromium 浏览器：
  1. 打开登录页，填账号密码；
  2. 点击极验验证按钮；
  3. 若是无感验证则自动通过；若弹出点选题，则用智谱免费视觉模型
     (glm-4v-flash) 截图识别并点击；
  4. 提交登录，拿新 cookie，POST /user/checkin 签到。
每次都是新登录，不依赖会过期的长期 cookie。

环境变量：
  IKUUU_EMAIL      必填，账号邮箱
  IKUUU_PASSWD     必填，账号密码
  IKUUU_DOMAINS    可选，逗号分隔域名，默认 ikuuu.top,ikuuu.pw,ikuuu.org
  ZHIPU_API_KEY    可选但强烈建议，智谱免费视觉模型 Key（用于点选验证码）
  SCKEY            可选，Server酱密钥
  PUSHPLUS_TOKEN   可选，PushPlus 密钥
"""
import base64
import io
import json
import os
import re
import sys
import time

import requests

# === 强制 UTF-8 输出 ===
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from playwright.sync_api import sync_playwright

# === 配置 ===
EMAIL = os.environ.get('IKUUU_EMAIL', '').strip()
PASSWD = os.environ.get('IKUUU_PASSWD', '').strip()
DOMAINS_RAW = os.environ.get('IKUUU_DOMAINS', '').strip()
DOMAINS = [d.strip() for d in DOMAINS_RAW.split(',') if d.strip()] if DOMAINS_RAW else [
    'ikuuu.top', 'ikuuu.pw', 'ikuuu.org',
]
ZHIPU_API_KEY = os.environ.get('ZHIPU_API_KEY', '').strip()
SCKEY = os.environ.get('SCKEY', '').strip()
PUSHPLUS_TOKEN = os.environ.get('PUSHPLUS_TOKEN', '').strip()

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')

MAX_CAPTCHA_RETRY = 3


def push(title, content):
    """推送通知（Server酱 / PushPlus 二选一）"""
    if SCKEY and SCKEY != '1':
        try:
            requests.post(f'https://sctapi.ftqq.com/{SCKEY}.send',
                          data={'title': title, 'desp': content}, timeout=10)
            print('Server酱推送完成')
        except Exception as e:
            print(f'推送失败: {e}')
    elif PUSHPLUS_TOKEN and PUSHPLUS_TOKEN != '1':
        try:
            requests.post('http://www.pushplus.plus/send',
                          json={'token': PUSHPLUS_TOKEN, 'title': title,
                                'content': content, 'template': 'json'}, timeout=10)
            print('PushPlus 推送完成')
        except Exception as e:
            print(f'推送失败: {e}')
    else:
        print('未配置消息推送')


def captcha_ready(page):
    """极验是否已经完成验证（页面 JS 暴露了 Captcha.isReady()）"""
    try:
        return bool(page.evaluate(
            "() => !!(window.Captcha && typeof window.Captcha.isReady === 'function'"
            " && window.Captcha.isReady())"))
    except Exception:
        return False


def click_first(page, selectors, timeout=2500):
    """依次尝试多个选择器，点到第一个可见元素就返回 True"""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=timeout)
                print(f'  点击元素: {sel}')
                return True
        except Exception:
            continue
    return False


def vlm_solve(page, domain):
    """用智谱免费视觉模型识别点选题并点击，返回是否通过"""
    if not ZHIPU_API_KEY:
        print('[验证码] 未配置 ZHIPU_API_KEY，无法自动识别点选题')
        return False

    from zhipuai import ZhipuAI
    client = ZhipuAI(api_key=ZHIPU_API_KEY)

    # 优先截验证码弹窗，找不到就截整页
    img_bytes = None
    for sel in ['.geetest_panel', '.geetest_popup', '.geetest_modal',
                '[class*="geetest_panel"]', 'body']:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                img_bytes = loc.screenshot()
                break
        except Exception:
            continue
    if img_bytes is None:
        img_bytes = page.screenshot()

    # 留下截图供排查（Actions 会作为 artifact 上传）
    with open(f'captcha_{domain.replace(".", "_")}.png', 'wb') as f:
        f.write(img_bytes)

    b64 = base64.b64encode(img_bytes).decode('utf-8')
    prompt = (
        '这是 GeeTest 验证码截图，可能是九宫格（从 3x3 图片中选出符合文字提示的物体）、'
        '图标点选或文字点选。\n'
        '请直接执行验证：输出需要点击的位置序号列表。\n'
        '九宫格编号规则：第一行 1、2、3，第二行 4、5、6，第三行 7、8、9。\n'
        '只返回 JSON 整数数组，例如 [3, 7]；无法判断返回 []；不要任何解释文字。'
    )
    try:
        resp = client.chat.completions.create(
            model='glm-4v-flash',
            messages=[{'role': 'user', 'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{b64}'}},
            ]}],
        )
        raw = (resp.choices[0].message.content or '').strip()
    except Exception as e:
        print(f'[验证码] 视觉模型调用失败: {e}')
        return False

    print(f'[验证码] 模型原始输出: {raw!r}')
    m = re.search(r'\[.*?\]', raw, re.DOTALL)
    if not m:
        return False
    try:
        cells = [int(x) for x in json.loads(m.group(0)) if isinstance(x, int)]
    except Exception:
        return False
    cells = [c for c in cells if 1 <= c <= 9]
    if not cells:
        return False

    print(f'[验证码] 准备点击格子: {cells}')
    for idx in cells:
        click_first(page, [
            f'.geetest_faces > div:nth-child({idx})',
            f'.geetest_face:nth-child({idx})',
            f'[class*="geetest_face"]:nth-child({idx})',
            f'.geetest_panel img:nth-child({idx})',
        ])

    # 确定按钮
    click_first(page, [
        '.geetest_commit', '[class*="geetest_commit"]',
        '.geetest_btn-submit', '[class*="submit"]',
    ])
    page.wait_for_timeout(2500)
    return captcha_ready(page)


def try_domain(domain):
    """在单个域名上完成登录 + 签到，返回 (是否成功, 消息)"""
    print(f'\n--- 域名 {domain} ---')
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox'],
        )
        context = browser.new_context(
            user_agent=UA, viewport={'width': 1280, 'height': 800}, locale='zh-CN',
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = context.new_page()

        page.goto(f'https://{domain}/auth/login', wait_until='domcontentloaded', timeout=30000)
        page.wait_for_timeout(2000)

        page.fill('#email', EMAIL)
        page.fill('#password', PASSWD)

        # 点极验按钮触发验证（无感验证点一下就过；点选题会弹窗）
        click_first(page, [
            '.geetest_btn_click', '.geetest_tip',
            '[class*="geetest_btn"]', '[class*="geetest"] button',
        ])

        ok = False
        for attempt in range(1, MAX_CAPTCHA_RETRY + 1):
            print(f'[验证码] 等待验证 {attempt}/{MAX_CAPTCHA_RETRY} ...')
            for _ in range(12):
                if captcha_ready(page):
                    ok = True
                    break
                page.wait_for_timeout(1000)
            if ok:
                print('[验证码] 已通过')
                break
            print('[验证码] 未通过，尝试视觉模型识别')
            if not vlm_solve(page, domain):
                # 重新点一次验证按钮再试
                click_first(page, [
                    '.geetest_refresh', '[class*="geetest_refresh"]',
                    '.geetest_btn_click', '.geetest_tip',
                ])
        if not ok:
            page.screenshot(path=f'captcha_fail_{domain.replace(".", "_")}.png')
            browser.close()
            return False, '验证码未通过（截图已保存）'

        # 提交登录
        click_first(page, ['button[type="submit"]', '.login', 'button.login'])
        page.wait_for_timeout(5000)

        # 判断是否登录成功
        href = page.url
        print(f'[登录] 当前页面: {href}')
        cookies = context.cookies()
        browser.close()

    if not cookies:
        return False, '未获取到 cookie'

    # 用拿到的 cookie 签到
    session = requests.Session()
    for c in cookies:
        if c.get('name') and c.get('value'):
            session.cookies.set(c['name'], c['value'], domain=domain)
    try:
        resp = session.post(
            f'https://{domain}/user/checkin',
            headers={'origin': f'https://{domain}', 'user-agent': UA,
                     'x-requested-with': 'XMLHttpRequest'},
            timeout=20,
        )
        result = resp.json()
    except Exception as e:
        return False, f'签到请求异常: {e}'

    print(f'[签到] 返回: {result}')
    msg = result.get('msg', str(result))
    ret = result.get('ret')
    if ret in (0, 1) or '成功' in msg or '重复' in msg or '已经' in msg:
        return True, msg
    # 登录响应用 phase=authenticated，签到若返回这个也算成功
    if result.get('phase') == 'authenticated':
        return True, msg
    return False, f'签到返回异常: {msg} (ret={ret})'


def main():
    print('=== iKUUU 每日签到（浏览器自动登录版）===')
    if not EMAIL or not PASSWD:
        print('[FATAL] 未设置 IKUUU_EMAIL / IKUUU_PASSWD')
        push('❌ ikuuu 签到失败', '未配置账号密码，请检查 Secrets')
        return False

    print(f'账号: {EMAIL}')
    print(f'候选域名: {", ".join(DOMAINS)}')
    print(f'视觉模型: {"glm-4v-flash" if ZHIPU_API_KEY else "未配置（仅支持无感验证）"}')

    for domain in DOMAINS:
        for attempt in range(1, 3):
            ok, msg = try_domain(domain)
            if ok:
                print(f'✅ 签到成功 [{domain}]: {msg}')
                push('✅ ikuuu 签到成功', f'域名: {domain}\n{msg}\n邮箱: {EMAIL}')
                return True
            print(f'[{domain}] 第 {attempt} 次失败: {msg}')
            if attempt < 2:
                time.sleep(3)

    print('[ERROR] 所有域名均签到失败')
    push('❌ ikuuu 签到失败',
         f'所有域名尝试完毕仍失败。\n账号: {EMAIL}\n'
         f'请到 Actions 下载 captcha_*.png 截图查看验证码类型。')
    return False


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
