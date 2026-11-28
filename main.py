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
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

try:
    from PIL import Image
except ImportError:
    Image = None

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
_LAST_DOM_DUMPED = False  # DOM 结构只 dump 一次，避免日志刷屏


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


def vlm_chat(prompt, images):
    """调用智谱 OpenAI 兼容接口，images 为图片字节列表，返回文本"""
    content = [{'type': 'text', 'text': prompt}]
    for img in images:
        if not img:
            continue
        b64 = base64.b64encode(img).decode('utf-8')
        content.append({'type': 'image_url',
                        'image_url': {'url': f'data:image/png;base64,{b64}'}})
    resp = requests.post(
        'https://open.bigmodel.cn/api/paas/v4/chat/completions',
        headers={'Authorization': f'Bearer {ZHIPU_API_KEY}',
                 'Content-Type': 'application/json'},
        json={'model': 'glm-4v-flash',
              'messages': [{'role': 'user', 'content': content}]},
        timeout=60,
    )
    result = resp.json()
    if 'error' in result:
        raise RuntimeError(result['error'])
    return (result['choices'][0]['message']['content'] or '').strip()


def parse_json_array(text):
    """从模型输出里提取 JSON 整数数组"""
    if not text:
        return []
    cleaned = re.sub(r'```[a-zA-Z]*', '', text).replace('```', '').strip()
    m = re.search(r'\[.*?\]', cleaned, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def parse_json_obj(text):
    """从模型输出里提取 JSON 对象"""
    if not text:
        return {}
    cleaned = re.sub(r'```[a-zA-Z]*', '', text).replace('```', '').strip()
    m = re.search(r'\{.*\}', cleaned, re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def dump_geetest_dom(page):
    """把页面上所有 geetest 相关元素的真实结构打印到日志（关键诊断）"""
    try:
        nodes = page.evaluate("""() => {
            const all = Array.from(document.querySelectorAll('*'));
            const out = [];
            for (const el of all) {
                const cls = (el.className && el.className.toString()) || '';
                if (!/geetest|captcha|verify/i.test(cls)) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 5 || r.height < 5) continue;
                const cs = getComputedStyle(el);
                out.push({
                    tag: el.tagName,
                    cls: cls.slice(0, 140),
                    x: Math.round(r.x), y: Math.round(r.y),
                    w: Math.round(r.width), h: Math.round(r.height),
                    vis: cs.visibility, disp: cs.display,
                    text: (el.innerText || '').trim().slice(0, 40)
                });
            }
            return out.slice(0, 60);
        }""")
        print(f'[诊断] geetest 元素共 {len(nodes)} 个:')
        for n in nodes:
            print(f"   {n['tag']:6s} [{n['x']},{n['y']} {n['w']}x{n['h']}] "
                  f"{n['cls']}" + (f" | {n['text']}" if n['text'] else ''))
    except Exception as e:
        print(f'[诊断] DOM dump 失败: {e}')


def find_grid_area(page):
    """启发式找九宫格区域 bbox（不依赖具体类名）。返回 dict 或 None"""
    try:
        cands = page.evaluate("""() => {
            const out = [];
            const nodes = document.querySelectorAll('div,section,canvas,img,ul');
            for (const el of nodes) {
                const r = el.getBoundingClientRect();
                const w = r.width, h = r.height;
                if (w < 140 || h < 140 || w > 480 || h > 480) continue;
                const aspect = w / h;
                if (aspect < 0.65 || aspect > 1.5) continue;
                if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue;
                const cs = getComputedStyle(el);
                if (cs.visibility === 'hidden' || cs.display === 'none' ||
                    parseFloat(cs.opacity || '1') < 0.5) continue;
                const kids = Array.from(el.children).filter(k => {
                    const kr = k.getBoundingClientRect();
                    return kr.width > 25 && kr.height > 25;
                }).length;
                const cls = (el.className && el.className.toString()) || '';
                let score = 0;
                if (kids >= 9 && kids <= 12) score += 80;
                else if (kids >= 4 && kids <= 16) score += 30;
                if (/geetest|table|grid|nine|captcha|puzzle/i.test(cls)) score += 50;
                if (w >= 200 && h >= 200) score += 10;
                out.push({x: r.x, y: r.y, w: w, h: h, kids: kids,
                          cls: cls.slice(0, 100), tag: el.tagName, score: score});
            }
            out.sort((a, b) => b.score - a.score);
            return out.slice(0, 10);
        }""")
    except Exception as e:
        print(f'[诊断] 区域探测失败: {e}')
        return None
    if not cands:
        return None
    print('[诊断] 九宫格候选区域:')
    for c in cands[:6]:
        print(f"   {c['tag']:6s} {c['w']:.0f}x{c['h']:.0f} kids={c['kids']} "
              f"score={c['score']} .{c['cls']}")
    best = cands[0]
    if best['score'] <= 0:
        return None
    return best


def clean_name(text):
    """把模型输出的物体名称清洗成短词"""
    if not text:
        return ''
    name = text.strip().splitlines()[0].strip()
    name = re.sub(r'^(图中是|图片中是|这是|答案是|物体名称是|名称是|一个|一只|一张)', '', name)
    name = re.sub(r'[^\w\u4e00-\u9fff]', '', name)
    return name[:12]


def wait_grid_loaded(page):
    """等待九宫格图片加载并稳定，避免截到空白/过渡态"""
    deadline = time.time() + 8
    last = None
    stable = 0
    while time.time() < deadline:
        try:
            state = page.evaluate("""() => {
                const box = document.querySelector('.geetest_table_box');
                if (!box) return null;
                const imgs = Array.from(box.querySelectorAll('img'));
                const bgs = Array.from(box.querySelectorAll('*'))
                    .map(el => getComputedStyle(el).backgroundImage)
                    .filter(v => v && v !== 'none');
                const opacity = parseFloat(getComputedStyle(box).opacity || '1');
                return {total: imgs.length,
                        complete: imgs.filter(i => i.complete && i.naturalWidth > 0).length,
                        bgCount: bgs.length, opacity: opacity};
            }""")
        except Exception:
            state = None
        if state:
            has_content = ((state['total'] > 0 and state['complete'] >= state['total'])
                           or state['bgCount'] > 0)
            if has_content and state['opacity'] >= 0.95:
                sig = (state['total'], state['complete'], state['bgCount'])
                stable = stable + 1 if sig == last else 1
                last = sig
                if stable >= 2:
                    page.wait_for_timeout(300)
                    return True
        page.wait_for_timeout(400)
    return False


def crop_cells(grid_bytes):
    """把九宫格整图切成 9 格（1-9）"""
    if Image is None:
        return []
    img = Image.open(io.BytesIO(grid_bytes))
    w, h = img.size
    cw, ch = w / 3.0, h / 3.0
    ix, iy = cw * 0.06, ch * 0.06
    cells = []
    for r in range(3):
        for c in range(3):
            cell = img.crop((int(c * cw + ix), int(r * ch + iy),
                             int((c + 1) * cw - ix), int((r + 1) * ch - iy)))
            buf = io.BytesIO()
            cell.save(buf, format='PNG')
            cells.append(buf.getvalue())
    return cells


def normalize_cells(items):
    """清洗格子编号：合法(1-9)、去重、有序；全选视为失控"""
    out = []
    for x in items:
        try:
            v = int(str(x).strip())
        except Exception:
            continue
        if 1 <= v <= 9 and v not in out:
            out.append(v)
    if len(out) >= 9:
        return []
    return sorted(out)


def describe_cells(cells):
    """并发识别 9 个格子的物体名称"""
    results = [None] * len(cells)

    def worker(i):
        raw = vlm_chat('这是九宫格验证码中的一个格子。只回答格子里物体的中文名称（2-6个字）。'
                       '空白或看不出物体只回答：空白。不要解释。', [cells[i]])
        return i, clean_name(raw) or '未知'

    with ThreadPoolExecutor(max_workers=3) as pool:
        for i, name in pool.map(worker, range(len(cells))):
            results[i] = name
    return results


def extract_target(page):
    """从页面文本里提取题目，如『请点击图中所有的猫』-> 猫"""
    try:
        body = page.locator('body').inner_text(timeout=1500)
    except Exception:
        return ''
    patterns = [
        r'请[点击选出找]*[图中]*所有[的是]?\s*[「"【]?([^，。,、\n\s]{1,10})',
        r'请[点击选出找]*([^，。,、\n\s]{2,10})[的图]',
        r'点击[^，。,、\n]{0,8}([一-龥]{1,8})',
    ]
    for pat in patterns:
        m = re.search(pat, body)
        if m:
            t = clean_name(m.group(1))
            if t and len(t) >= 1:
                print(f'[验证码] 文字题目: {t!r}')
                return t
    return ''


def vlm_solve(page, domain):
    """用智谱视觉模型识别点选验证码并点击，返回是否通过。
    不依赖具体 CSS 类名：先启发式定位九宫格区域，定位不到就用整页截图 +
    VLM 输出归一化坐标点击。同时把真实 DOM 结构打印到日志便于诊断。"""
    global _LAST_DOM_DUMPED
    if not ZHIPU_API_KEY:
        print('[验证码] 未配置 ZHIPU_API_KEY')
        return False

    safe = domain.replace('.', '_')

    # 关键诊断：第一次进入时把真实 DOM 结构打出来
    if not _LAST_DOM_DUMPED:
        dump_geetest_dom(page)
        _LAST_DOM_DUMPED = True

    page.wait_for_timeout(600)  # 等弹窗动画稳定

    # 1. 先截整页（题目和九宫格都在里面），保存供排查
    try:
        full_bytes = page.screenshot()
        with open(f'captcha_{safe}.png', 'wb') as f:
            f.write(full_bytes)
    except Exception as e:
        print(f'[验证码] 整页截图失败: {e}')
        return False

    # 2. 启发式定位九宫格区域
    grid = find_grid_area(page)
    target = extract_target(page)
    print(f'[验证码] 题目: {target or "(未提取到，让模型自行判断)"}')

    # 3. 让模型直接给出要点击的格子在截图中的归一化坐标(0-1000)
    #    这样彻底绕开 DOM 类名和 bounding box 计算
    hint = f'题目要求找出【{target}】。' if target else ''
    try:
        raw = vlm_chat(
            '这是一张 GeeTest 点选验证码截图，中央有一个 3x3 九宫格图片，'
            '上方有文字题目说明要点击什么。' + hint +
            '请仔细观察题目和九宫格，找出所有符合题意的格子。\n'
            '以 0-1000 的归一化坐标返回每个要点击格子的中心点（左上角为 0,0，右下角为 1000,1000）。\n'
            '另外返回九宫格区域边界框。只返回 JSON，例如：\n'
            '{"box":[300,250,700,650],"points":[[380,330],[620,570]]}\n'
            '没有符合的格子 points 返回 []，不要解释。',
            [full_bytes])
        print(f'[验证码] 模型输出: {raw!r}')
    except Exception as e:
        print(f'[验证码] 模型调用失败: {e}')
        return False

    data = parse_json_obj(raw)
    points = data.get('points') or []
    validated = []
    for p in points:
        try:
            x, y = float(p[0]), float(p[1])
            if 0 <= x <= 1000 and 0 <= y <= 1000:
                validated.append((x, y))
        except Exception:
            continue
    if not validated:
        print('[验证码] 模型未给出有效坐标，本轮放弃')
        return False

    # 4. 把归一化坐标映射到页面像素并点击
    vw, vh = page.viewport_size['width'], page.viewport_size['height']
    print(f'[验证码] 点击 {len(validated)} 个坐标 (视口 {vw}x{vh})')
    for nx, ny in validated:
        x = nx / 1000.0 * vw
        y = ny / 1000.0 * vh
        page.mouse.click(x, y)
        time.sleep(random.uniform(0.25, 0.45))

    # 5. 提交（多类名兜底）
    page.wait_for_timeout(300)
    click_first(page, [
        '.geetest_commit', '.geetest_submit', '.geetest_btn_submit',
        '[class*="commit"]', '[class*="submit"]', 'text=确认', 'text=提交',
    ])
    page.wait_for_timeout(2000)

    if captcha_ready(page):
        print('[验证码] 通过')
        return True
    body = ''
    try:
        body = page.locator('body').inner_text(timeout=1500)
    except Exception:
        pass
    if any(h in body for h in ['验证失败', '再试一次', '请再试一次']):
        print('[验证码] 校验失败，需刷新')
    return False


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

        # 无感验证：最多等 3 秒
        ok = False
        for _ in range(6):
            if captcha_ready(page):
                ok = True
                break
            page.wait_for_timeout(500)

        # 点选题：用视觉模型多轮尝试
        if not ok:
            for attempt in range(1, MAX_CAPTCHA_RETRY + 1):
                print(f'[验证码] 视觉识别 {attempt}/{MAX_CAPTCHA_RETRY} ...')
                if vlm_solve(page, domain):
                    ok = True
                    break
                # 刷新验证码再试：多类名兜底；实在找不到就重新点验证按钮
                refreshed = click_first(page, [
                    '.geetest_refresh', '[class*="refresh"]',
                    '.geetest_btn_click', '[class*="geetest_btn"]',
                ])
                if not refreshed:
                    print('[验证码] 未找到刷新按钮，重新点验证按钮')
                    click_first(page, ['.geetest_btn_click', '[class*="geetest_btn"]'])
                page.wait_for_timeout(1200)

        if not ok:
            try:
                page.screenshot(path=f'captcha_fail_{domain.replace(".", "_")}.png')
            except Exception:
                pass
            browser.close()
            return False, '验证码未通过（截图已保存）'
        print('[验证码] 已通过')

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
            try:
                ok, msg = try_domain(domain)
            except Exception as e:
                ok, msg = False, f'异常: {type(e).__name__}: {e}'
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
