#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iKUUU 机场每日签到 - 浏览器自动登录版

ikuuu 现在随机返回三种 GeeTest V4 验证码：
  nine  九宫格（选 N 个与参考图标相同的格子）
  click 文字点选（按题目顺序点击指定的字）
  icon  图标点选（暂不处理，答错后会自动换题）

v3 修复要点（对照 v2 的失败日志）：
  1. 先读 DOM 判定验证码类型，不再"一套逻辑打天下"；
  2. 九宫格改为「命名 + 文本匹配 + 按元素点击」：
     参考图标和 9 个格子分别截图，让视觉模型只输出物体名称，
     再按名称匹配选格子，最后用 Playwright 点 .geetest_item_wrap[i]。
     全程零坐标回归，彻底绕开模型编坐标的问题；
  3. 九宫格题目是图标而不是文字，不再从 body 里正则抓文字当题目；
  4. 从"选 3 个符合右图的图片"里解析出需要选几个，严格点够数量；
  5. 文字点选只截验证码区域，按题目字符逐个问归一化坐标（0-1），
     再映射回该元素的 CSS 盒子，避免全页坐标和 DPR 偏差；
  6. 答错后极验会自动换新题，重试时只等新题并重新判型，
     不再盲目点 .geetest_refresh。

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
# 视觉模型调用总量上限：一次运行最多花这么多次，防止验证码连续失败时烧穿免费额度
MAX_VLM_CALLS = 80
_vlm_calls = 0

# 九宫格里这些回答代表"没有可用物体"，不参与匹配
EMPTY_WORDS = {'空白', '未知', '无', '没有', '看不出', '空'}


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
    global _vlm_calls
    if _vlm_calls >= MAX_VLM_CALLS:
        raise RuntimeError(f'视觉模型调用已达上限 {MAX_VLM_CALLS} 次')
    _vlm_calls += 1
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


def clean_name(text):
    """把模型输出的物体名称清洗成短词"""
    if not text:
        return ''
    # 先去掉 markdown 代码块围栏，否则首行会变成 "json"
    t = re.sub(r'```[a-zA-Z]*', '', text).replace('```', '').strip()
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if not lines:
        return ''
    name = lines[0]
    name = re.sub(r'^(图中是|图片中是|这是|答案是|物体名称是|名称是|一个|一只|一张|图标是)', '', name)
    name = re.sub(r'[^\w\u4e00-\u9fff]', '', name)
    return name[:12]


# ---------------------------------------------------------------- 图像工具

def upscale_png(data, factor=3, max_side=1400):
    """放大图片，让小图标/小字能被视觉模型看清"""
    if not data or Image is None:
        return data
    try:
        img = Image.open(io.BytesIO(data)).convert('RGB')
    except Exception:
        return data
    w, h = img.size
    if w < 8 or h < 8:
        return data
    f = factor
    if max(w, h) * f > max_side:
        f = max(1.0, max_side / float(max(w, h)))
    if f > 1.05:
        img = img.resize((max(1, int(w * f)), max(1, int(h * f))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


def fetch_image(url, referer='https://ikuuu.top/'):
    """下载极验的背景图资源（原始分辨率，比元素截图更清晰）"""
    if not url or not url.startswith('http'):
        return None
    try:
        r = requests.get(url, timeout=15,
                         headers={'user-agent': UA, 'referer': referer})
        if r.ok and len(r.content) > 100:
            return r.content
    except Exception:
        pass
    return None


def shot_el(page, selector, idx=0):
    """对第 idx 个匹配元素截图，失败返回 None"""
    try:
        loc = page.locator(selector)
        if loc.count() <= idx:
            return None
        return loc.nth(idx).screenshot(timeout=3000)
    except Exception:
        return None


# ---------------------------------------------------------------- DOM 判型

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


def captcha_state(page):
    """一次性读出验证码类型、区域、格子、题目等结构化信息"""
    try:
        return page.evaluate("""() => {
            const q = s => document.querySelector(s);
            const rect = el => {
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return {x: r.x, y: r.y, w: r.width, h: r.height};
            };
            const bgOf = el => {
                if (!el) return '';
                const v = getComputedStyle(el).backgroundImage || '';
                const m = v.match(/url\\(["']?(.*?)["']?\\)/);
                return m ? m[1] : '';
            };
            // 极验 V4 参考图在 <img src>（原图 200x200），background-image 恒为 none。
            // 只取 background 会导致参考图永远为空，必须同时取 img src。
            const refSrcOf = el => {
                if (!el) return '';
                const img = el.querySelector('img');
                return img ? (img.currentSrc || img.src || '') : '';
            };
            const nineEl = q('.geetest_subitem.geetest_nine');
            const clickEl = q('.geetest_subitem.geetest_click');
            const iconEl = q('.geetest_subitem.geetest_icon');
            const winEl = q('.geetest_window');
            const bgEl = q('.geetest_bg');
            const tipsEl = q('.geetest_text_tips');
            const quesEl = q('.geetest_ques_tips');
            const tiles = Array.from(document.querySelectorAll('.geetest_item_wrap'));
            return {
                nine: !!nineEl, click: !!clickEl, icon: !!iconEl,
                nineRect: rect(nineEl), clickRect: rect(clickEl),
                winRect: rect(winEl), bgRect: rect(bgEl),
                tileCount: tiles.length,
                tips: tipsEl ? (tipsEl.innerText || '').trim() : '',
                quesBg: bgOf(quesEl),
                quesSrc: refSrcOf(quesEl),
                quesRect: rect(quesEl),
                hasQues: !!quesEl,
                commit: !!q('.geetest_commit'),
                bodyHint: (document.body.innerText || '').slice(0, 200),
            };
        }""")
    except Exception as e:
        print(f'[诊断] 读取验证码状态失败: {e}')
        return {}


def detect_kind(st):
    if st.get('nine'):
        return 'nine'
    if st.get('click'):
        return 'click'
    if st.get('icon'):
        return 'icon'
    return None


# ---------------------------------------------------------------- 文本匹配

def parse_count(tips):
    """从「选 3 个符合右图的图片」里解析出需要选几个"""
    m = re.search(r'(\d+)\s*个', tips or '')
    if m:
        n = int(m.group(1))
        if 1 <= n <= 9:
            return n
    return 3


def parse_click_chars(tips):
    """从「请依次点击『在』『下』」里解析出要点的字（保持顺序）"""
    if not tips:
        return []
    quoted = re.findall(r'[『「“]([^』」”]{1,6})[』」”]', tips)
    if quoted:
        chars = []
        for g in quoted:
            chars.extend(list(g))
        return chars[:6]
    # 兜底：去掉指令性词语后剩下的汉字
    t = re.sub(r'(请|依次|按照|顺序|点击|选出|图中|文字|下方|上方|的|所有|并|确认)', '', tips)
    t = re.sub(r'[^\u4e00-\u9fff]', '', t)
    return list(t)[:6]


def char_overlap(a, b):
    """按字符集合算相似度（中文物体名很短，Jaccard 够用）"""
    sa, sb = set(a or ''), set(b or '')
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def score_cells(ref_name, names):
    """按名称相似度给每个格子打分，返回 [(score, idx, has_object), ...] 降序"""
    scored = []
    for i, n in enumerate(names):
        if not n or n in EMPTY_WORDS:
            scored.append((0.0, i, False))
            continue
        if n == ref_name:
            s = 1.0
        elif ref_name in n or n in ref_name:
            s = 0.9
        else:
            s = char_overlap(ref_name, n)
        scored.append((s, i, True))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored


def is_same_object(ref_img, cell_img):
    """直接比对：参考图和这个格子里是不是同类物体（比名称匹配更抗同义词）"""
    if not ref_img or not cell_img:
        return False
    try:
        raw = vlm_chat(
            '第一张图是参考物体，第二张图是九宫格中的一个格子。\n'
            '判断第二张图里是否存在和第一张图相同或同类的物体。\n'
            '只回答一个字：是 或 否。不要解释。', [ref_img, cell_img])
    except Exception as e:
        print(f'[验证码] 比对调用失败: {e}')
        return False
    if '否' in raw or '不' in raw or '无' in raw:
        return False
    return '是' in raw


# ---------------------------------------------------------------- 识别子任务

def describe_cells(cells):
    """并发识别每个格子的物体名称"""
    results = ['未知'] * len(cells)

    def worker(i):
        img = cells[i]
        if not img:
            return i, '未知'
        try:
            raw = vlm_chat(
                '这是九宫格验证码里的一个格子。只回答格子里最主要物体的中文名称，2-6个字。'
                '如果有多个物体，只回答最显眼的一个。空白或看不出物体只回答：空白。'
                '不要解释，不要标点。', [img])
            return i, (clean_name(raw) or '未知')
        except Exception as e:
            print(f'[验证码] 格子 {i + 1} 识别失败: {e}')
            return i, '未知'

    with ThreadPoolExecutor(max_workers=3) as pool:
        for i, name in pool.map(worker, range(len(cells))):
            results[i] = name
    return results


def locate_char(img_png, ch):
    """在验证码图里定位某个字，返回归一化坐标 (nx, ny)，失败返回 (None, None)"""
    prompt = (
        f'这是一张中文点选验证码图片，白色背景上散布着若干汉字。\n'
        f'请找到字符「{ch}」的位置。\n'
        f'用相对坐标回答：左上角是 (0,0)，右下角是 (1,1)，取该字中心点。\n'
        f'只返回 JSON，例如：{{"x":0.42,"y":0.58}}\n'
        f'如果图里没有这个字，返回 {{"x":null,"y":null}}。不要解释。'
    )
    try:
        raw = vlm_chat(prompt, [img_png])
    except Exception as e:
        print(f'[验证码] 定位「{ch}」失败: {e}')
        return None, None
    d = parse_json_obj(raw)
    try:
        x, y = float(d.get('x')), float(d.get('y'))
        if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
            return x, y
    except Exception:
        pass
    # 兜底：从任意文本里抓两个 0-1 之间的小数
    nums = re.findall(r'0\.\d+|1\.0+|(?<!\d)[01](?!\d)', raw or '')
    try:
        if len(nums) >= 2:
            x, y = float(nums[0]), float(nums[1])
            if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
                return x, y
    except Exception:
        pass
    return None, None


# ---------------------------------------------------------------- 两类验证码

def solve_nine(page, st):
    """九宫格：命名参考图 + 命名 9 格 + 文本匹配 + 按元素点击（零坐标）"""
    need = parse_count(st.get('tips'))
    print(f'[验证码] 九宫格，需选 {need} 个 | 原始题目: {st.get("tips")!r}')

    # 1) 参考图标：优先从 <img src> 下载 200x200 原图（最清晰），
    #    再试 background-image，最后才退化到元素截图（72x24，很小）。
    ref = fetch_image(st.get('quesSrc')) or fetch_image(st.get('quesBg'))
    if not ref:
        ref = shot_el(page, '.geetest_ques_tips')
    if not ref:
        print('[验证码] 未取到参考图，放弃本轮')
        return False
    ref = upscale_png(ref, 4, 600)

    try:
        ref_name = clean_name(vlm_chat(
            '这是验证码的参考图标。只回答图中物体的中文名称，2-6个字，'
            '不要解释、不要标点。看不出就回答：未知。', [ref]))
    except Exception as e:
        print(f'[验证码] 参考图识别失败: {e}')
        return False
    if not ref_name or ref_name in EMPTY_WORDS:
        print(f'[验证码] 参考图无法识别: {ref_name!r}')
        return False
    print(f'[验证码] 参考物体: {ref_name}')

    # 2) 逐格截图（每格单独截，避免整图切分错位）
    n = st.get('tileCount') or 0
    if n < 9:
        print(f'[验证码] 格子数量异常: {n}')
        return False
    cells = []
    for i in range(n):
        cells.append(upscale_png(shot_el(page, '.geetest_item_wrap', i), 3, 400))

    # 3) 逐格命名
    names = describe_cells(cells)
    print(f'[验证码] 格子内容: {names}')

    # 4) 名称匹配（≥0.5 视为可信）
    scored = score_cells(ref_name, names)
    idx = [i for s, i, _ in scored if s >= 0.5][:need]
    print(f'[验证码] 名称命中: {[i + 1 for i in idx]}')

    # 5) 不够就逐格直接比对补足（抗"闹钟/时钟"这类同义词错配）
    if len(idx) < need:
        print(f'[验证码] 匹配 {len(idx)}/{need}，改用逐格比对补足')
        for s, i, has_obj in scored:
            if len(idx) >= need:
                break
            if i in idx or not has_obj:
                continue
            if is_same_object(ref, cells[i]):
                idx.append(i)
                print(f'  比对命中格子 {i + 1}')
        idx = sorted(idx)

    # 6) 还差就用分数最高的非空格子凑数（九宫格必须点够 N 个）
    if len(idx) < need:
        for s, i, has_obj in scored:
            if len(idx) >= need:
                break
            if i not in idx and has_obj:
                idx.append(i)
        idx = sorted(idx)
    idx = idx[:need]
    print(f'[验证码] 最终点击格子: {[i + 1 for i in idx]}')

    if not idx:
        return False

    # 7) 按 DOM 元素点击，不做任何坐标换算
    for i in idx:
        tile = page.locator('.geetest_item_wrap').nth(i)
        try:
            tile.click(timeout=3000)
            print(f'  点击格子 {i + 1}')
        except Exception:
            # 格子上面盖着 geetest_ghost 覆盖层时常规点击会被判"被遮挡"，强制点兜底
            try:
                tile.click(timeout=2000, force=True)
                print(f'  点击格子 {i + 1}（force）')
            except Exception as e:
                print(f'  点击格子 {i + 1} 失败: {e}')
        time.sleep(random.uniform(0.3, 0.6))

    # 少数变体需要手动确认
    if st.get('commit'):
        page.wait_for_timeout(300)
        click_first(page, ['.geetest_commit', '.geetest_btn_commit'])
    return True


def recognize_ref_chars(st, page):
    """从参考图识别要依次点击的汉字。
    ikuuu 的 tips 只是『请在下图依次点击』，不含具体字；
    真正的字在 geetest_ques_tips 的 <img>（200x200 原图）里，如『炒肝片』。"""
    ref = fetch_image(st.get('quesSrc')) or fetch_image(st.get('quesBg'))
    if not ref:
        ref = shot_el(page, '.geetest_ques_tips')
    if not ref:
        print('[验证码] 参考图（待点字）获取失败')
        return []
    ref = upscale_png(ref, 3, 600)
    try:
        raw = vlm_chat(
            '这是极验文字点选验证码的「提示图」，图里写着几个汉字，'
            '要求按顺序在下方大图里依次点击这些字。\n'
            '请从左到右准确读出图中所有汉字（按阅读顺序），只输出这些汉字本身。\n'
            '不要解释、不要标点、不要引号。例如图里是「炒肝片」就只回答：炒肝片',
            [ref])
    except Exception as e:
        print(f'[验证码] 参考图文字识别失败: {e}')
        return []
    # 只保留汉字，去掉模型可能的标点/说明
    chars = re.findall(r'[\u4e00-\u9fff]', raw or '')
    # 过滤常见的说明性词（若模型没听话）
    chars = [c for c in chars if c not in '图中是这的是字依次点击请按顺序汉字如下']
    print(f'[验证码] 参考图识别待点字: {chars} (原始: {raw!r})')
    return chars[:6]


def solve_click(page, st):
    """文字点选：从参考图读出要点的字，逐个在验证码大图里定位并点击"""
    box = st.get('winRect') or st.get('clickRect') or st.get('bgRect')
    if not box or box.get('w', 0) < 40:
        print('[验证码] 未取到点选区域')
        return False

    img = shot_el(page, '.geetest_window') or shot_el(page, '.geetest_bg')
    if not img:
        print('[验证码] 点选区域截图失败')
        return False
    img = upscale_png(img, 3, 1200)

    # 关键修复：要点的字来自参考图，不是 tips 文字
    chars = recognize_ref_chars(st, page)
    if not chars:
        # 兜底：仅当 tips 里真的带引号字时才用它（某些变体）
        chars = parse_click_chars(st.get('tips'))
        print(f'[验证码] 参考图无字，改用 tips: {chars}')
    if not chars:
        return False

    for ch in chars:
        nx, ny = locate_char(img, ch)
        if nx is None:
            print(f'[验证码] 未定位到「{ch}」，放弃本轮')
            return False
        x = box['x'] + nx * box['w']
        y = box['y'] + ny * box['h']
        page.mouse.click(x, y)
        print(f'  点击「{ch}」@ ({x:.0f},{y:.0f})')
        time.sleep(random.uniform(0.3, 0.6))

    page.wait_for_timeout(300)
    if not click_first(page, ['.geetest_commit', '.geetest_btn_commit',
                              '.geetest_commit_btn', 'text=确定']):
        print('[验证码] 未找到确认按钮（部分变体自动提交）')
    return True


def vlm_solve(page, domain):
    """判定验证码类型并求解，返回是否通过"""
    global _LAST_DOM_DUMPED
    if not ZHIPU_API_KEY:
        print('[验证码] 未配置 ZHIPU_API_KEY')
        return False

    safe = domain.replace('.', '_')

    if not _LAST_DOM_DUMPED:
        dump_geetest_dom(page)
        _LAST_DOM_DUMPED = True

    page.wait_for_timeout(600)  # 等弹窗动画稳定

    # 整页截图留档，便于事后排查
    try:
        with open(f'captcha_{safe}.png', 'wb') as f:
            f.write(page.screenshot())
    except Exception as e:
        print(f'[验证码] 整页截图失败: {e}')

    st = captcha_state(page)
    kind = detect_kind(st)
    print(f'[验证码] 类型: {kind or "未识别"}')

    if kind == 'nine':
        ok = solve_nine(page, st)
    elif kind == 'click':
        ok = solve_click(page, st)
    elif kind == 'icon':
        print('[验证码] 图标点选暂不支持，等换题')
        ok = False
    else:
        print('[验证码] 未识别的验证码类型')
        ok = False

    if not ok:
        return False

    # 等极验回调结果
    for _ in range(10):
        if captcha_ready(page):
            print('[验证码] 通过')
            return True
        page.wait_for_timeout(400)
    return False


def try_domain(domain):
    """在单个域名上完成登录 + 签到，返回 (是否成功, 消息)"""
    print(f'\n--- 域名 {domain} ---')
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox'],
        )
        # device_scale_factor=2：截图清晰一倍，mouse.click 仍用 CSS 像素，互不影响
        context = browser.new_context(
            user_agent=UA, viewport={'width': 1280, 'height': 800},
            device_scale_factor=2, locale='zh-CN',
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
                # 极验答错后会自动换新题，这里只等新题出现并重新判型；
                # 只有整个面板都消失了才需要重新点验证按钮。
                page.wait_for_timeout(1500)
                st = captcha_state(page)
                if not detect_kind(st):
                    print('[验证码] 面板已关闭，重新点验证按钮')
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
