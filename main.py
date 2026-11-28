#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iKUUU 机场每日签到 - 浏览器自动登录版

ikuuu 现在随机返回三种 GeeTest V4 验证码：
  nine  九宫格（选 N 个与参考图标相同的格子）
  click 文字点选（按参考图顺序点击指定的字）
  icon  图标点选（暂不处理，答错后会自动换题）

v4 修复要点（对照 v3 的失败日志）：
  1. 参考图必须等真正加载完：占位状态下 ques_tips 里是随机字母
     （日志里的 yqkx / jfdl），此时识别必然得到一堆幻觉生僻字
     （锝/镉/钡）。现在识别到纯字母就等待重试，最多 3 次。
  2. 点选题彻底放弃「让 VLM 输出坐标」——GLM-4V 没有定位能力，
     v3 日志里三次点击 x 全是 640（正好是 300 宽区域的中线）。
     改为：ddddocr 检测文字框 → 逐框裁图放大 → VLM 只认单字
     → 按字匹配框 → 点框中心。坐标全部来自 OCR，模型只负责认字。
  3. 目标字在检测结果里找不到 → 说明参考图识别错了，点刷新换一道题，
     不硬猜。刷新最多 3 次，避免触发风控。
  4. 单字识别结果去重保序，避免模型复读把日志刷爆（v3 出现过 200 个「锝」）。
  5. 重试次数上调：验证码本质是概率游戏。

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

try:
    import ddddocr
except Exception:
    ddddocr = None

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

MAX_CAPTCHA_RETRY = 5   # 单个域名内，验证码最多重试几轮
DOMAIN_RETRY = 2        # 单个域名最多整体尝试几次
MAX_REFRESH = 3         # 识别不确定时最多刷新换题几次
_LAST_DOM_DUMPED = False
# 视觉模型调用总量上限，防止连续失败烧穿免费额度
MAX_VLM_CALLS = 500
_vlm_calls = 0

# 九宫格里这些回答代表"没有可用物体"，不参与匹配
EMPTY_WORDS = {'空白', '未知', '无', '没有', '看不出', '空',
               '白色背景', '纯色', '背景', '纯白'}


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


# ---------------------------------------------------------------- 视觉模型

def vlm_chat(prompt, images, retries=2):
    """调用智谱 OpenAI 兼容接口，images 为图片字节列表，返回文本"""
    global _vlm_calls
    if _vlm_calls >= MAX_VLM_CALLS:
        print(f'[验证码] 视觉模型调用已达上限 {MAX_VLM_CALLS} 次，跳过')
        return ''
    content = [{'type': 'text', 'text': prompt}]
    for img in images:
        if not img:
            continue
        b64 = base64.b64encode(img).decode('utf-8')
        content.append({'type': 'image_url',
                        'image_url': {'url': f'data:image/png;base64,{b64}'}})

    last_err = None
    for i in range(retries + 1):
        _vlm_calls += 1
        try:
            resp = requests.post(
                'https://open.bigmodel.cn/api/paas/v4/chat/completions',
                headers={'Authorization': f'Bearer {ZHIPU_API_KEY}',
                         'Content-Type': 'application/json'},
                json={'model': 'glm-4v-flash', 'temperature': 0.01,
                      'messages': [{'role': 'user', 'content': content}]},
                timeout=60,
            )
            result = resp.json()
            if 'error' in result:
                msg = str(result['error'])
                last_err = msg
                # 429 / 限流：退避后重试
                if i < retries and ('429' in msg or 'rate' in msg.lower()
                                    or '频繁' in msg or '1305' in msg):
                    time.sleep(1.5 * (i + 1))
                    continue
                print(f'[验证码] 视觉模型返回错误: {msg[:160]}')
                return ''
            return (result['choices'][0]['message']['content'] or '').strip()
        except Exception as e:
            last_err = f'{type(e).__name__}: {e}'
            if i < retries:
                time.sleep(1.5 * (i + 1))
                continue
    print(f'[验证码] 视觉模型调用失败: {last_err}')
    return ''


def clean_name(text):
    """把模型输出的物体名称清洗成短词"""
    if not text:
        return ''
    t = re.sub(r'```[a-zA-Z]*', '', text).replace('```', '').strip()
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if not lines:
        return ''
    name = lines[0]
    name = re.sub(r'^(图中是|图片中是|这是|答案是|物体名称是|名称是|一个|一只|一张|图标是)', '', name)
    name = re.sub(r'[^\w\u4e00-\u9fff]', '', name)
    return name[:12]


# ---------------------------------------------------------------- 图像工具

def open_img(data):
    """PNG 字节 -> PIL Image"""
    if not data or Image is None:
        return None
    try:
        return Image.open(io.BytesIO(data)).convert('RGB')
    except Exception:
        return None


def img_to_png(img):
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


def upscale_png(data, factor=3, max_side=1400):
    """放大图片，让小图标/小字能被视觉模型看清"""
    img = open_img(data)
    if img is None:
        return data
    w, h = img.size
    if w < 8 or h < 8:
        return data
    f = float(factor)
    if max(w, h) * f > max_side:
        f = max(1.0, max_side / float(max(w, h)))
    if f <= 1.05:
        return data
    img = img.resize((max(1, int(w * f)), max(1, int(h * f))), Image.LANCZOS)
    return img_to_png(img)


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


# ---------------------------------------------------------------- OCR 定位

_DET = None
_DET_TRIED = False
_OCR = None
_OCR_TRIED = False


def get_det():
    """懒加载 ddddocr 的文字检测器"""
    global _DET, _DET_TRIED
    if _DET_TRIED:
        return _DET
    _DET_TRIED = True
    if ddddocr is None:
        print('[验证码] 未安装 ddddocr，点选题只能靠模型猜坐标（成功率很低）')
        return None
    for kwargs in ({'det': True, 'show_ad': False}, {'det': True}):
        try:
            _DET = ddddocr.DdddOcr(**kwargs)
            print('[验证码] 文字检测器已就绪 (ddddocr)')
            return _DET
        except TypeError:
            continue
        except Exception as e:
            print(f'[验证码] 文字检测器初始化失败: {type(e).__name__}: {e}')
            return None
    return None


def get_ocr():
    """懒加载 ddddocr 的单字分类器（免费，比调 API 快得多）"""
    global _OCR, _OCR_TRIED
    if _OCR_TRIED:
        return _OCR
    _OCR_TRIED = True
    if ddddocr is None:
        return None
    for kwargs in ({'show_ad': False}, {}):
        try:
            _OCR = ddddocr.DdddOcr(**kwargs)
            print('[验证码] 单字分类器已就绪 (ddddocr)')
            return _OCR
        except TypeError:
            continue
        except Exception as e:
            print(f'[验证码] 单字分类器初始化失败: {type(e).__name__}: {e}')
            return None
    return None


def ocr_boxes(img_bytes):
    """文字检测，返回 [[x1,y1,x2,y2], ...]（相对传入图片的像素）"""
    det = get_det()
    if det is None or not img_bytes:
        return []
    try:
        res = det.detection(img_bytes)
        return [b for b in (res or []) if len(b) >= 4]
    except Exception as e:
        print(f'[验证码] 文字检测异常: {type(e).__name__}: {e}')
        return []


def ocr_boxes_best(img_bytes):
    """检测文字框。原图对比度低时再试一次灰度增强版，取框更多的那个。"""
    boxes = ocr_boxes(img_bytes)
    img = open_img(img_bytes)
    if img is None or Image is None:
        return boxes
    try:
        from PIL import ImageOps
        gray = ImageOps.autocontrast(img.convert('L'), cutoff=2).convert('RGB')
        boxes2 = ocr_boxes(img_to_png(gray))
        if len(boxes2) > len(boxes):
            print(f'[验证码] 增强后检出更多文字框: {len(boxes)} -> {len(boxes2)}')
            return boxes2
    except Exception:
        pass
    return boxes


def classify_char(img_bytes):
    """ddddocr 单字分类（免费），失败返回空串"""
    ocr = get_ocr()
    if ocr is None or not img_bytes:
        return ''
    try:
        r = (ocr.classification(img_bytes) or '').strip()
    except Exception:
        return ''
    # 只接受单个汉字，多字说明这一块框住了两个以上字符
    cs = re.findall(r'[\u4e00-\u9fff]', r)
    return cs[0] if len(cs) == 1 else ''


def recognize_single_char(img_bytes):
    """识别裁出来的单个汉字"""
    raw = vlm_chat(
        '这是从中文验证码图片上裁下来的单个汉字，笔画可能有描边、变形或轻微倾斜。'
        '只回答这个字本身，一个字。不要解释、不要标点、不要拼音。', [img_bytes])
    if not raw:
        return ''
    cs = re.findall(r'[\u4e00-\u9fff]', raw)
    return cs[0] if cs else ''


def recognize_many(crops):
    """并发识别一批单字裁图"""
    results = [''] * len(crops)
    if not crops:
        return results

    def worker(i):
        return i, recognize_single_char(crops[i])

    with ThreadPoolExecutor(max_workers=4) as pool:
        for i, ch in pool.map(worker, range(len(crops))):
            results[i] = ch
    return results


def detect_chars(img_bytes, css_w, css_h, scale=2):
    """
    对点选大图做「检测 + 裁剪 + 免费分类」。
    返回 [{cx, cy, crop, name}, ...]，坐标为相对该元素的 CSS 像素。
    坐标链路：det 坐标 / scale -> 截图设备像素 -> * (css/设备像素) -> CSS
    """
    base = open_img(img_bytes)
    if base is None:
        return []
    w, h = base.size
    if w < 20 or h < 20:
        return []
    sx = css_w / float(w) if css_w else 1.0
    sy = css_h / float(h) if css_h else 1.0

    big = base.resize((w * scale, h * scale), Image.LANCZOS)
    boxes = ocr_boxes_best(img_to_png(big))
    print(f'[验证码] 文字检测到 {len(boxes)} 个框')
    if not boxes:
        return []

    items = []
    widths = []
    for b in boxes:
        x1, y1, x2, y2 = [float(v) for v in b[:4]]
        # 还原到 base（设备像素）
        bx1, by1 = x1 / scale, y1 / scale
        bx2, by2 = x2 / scale, y2 / scale
        bw, bh = bx2 - bx1, by2 - by1
        # 过滤明显不合理的框（整图、细线、极小噪点）
        if bw < 8 or bh < 8 or bw > w * 0.92 or bh > h * 0.92:
            continue
        pad = max(2, int(min(bw, bh) * 0.14))
        left = max(0, int(bx1 - pad))
        top = max(0, int(by1 - pad))
        right = min(w, int(bx2 + pad))
        bottom = min(h, int(by2 + pad))
        if right - left < 8 or bottom - top < 8:
            continue
        crop = base.crop((left, top, right, bottom))
        cw, chh = crop.size
        f = max(2.0, min(6.0, 96.0 / float(max(1, min(cw, chh)))))
        crop = crop.resize((int(cw * f), int(chh * f)), Image.LANCZOS)
        png = img_to_png(crop)
        items.append({
            'cx': ((bx1 + bx2) / 2.0) * sx,
            'cy': ((by1 + by2) / 2.0) * sy,
            'crop': png,
            'name': classify_char(png),
            'w': bw,
            'wide': False,
        })
        widths.append(bw)

    # 一个框圈住两个字的（宽度明显超过中位数）ddddocr 会认错，标记为不可信
    if widths:
        widths_sorted = sorted(widths)
        median_w = widths_sorted[len(widths_sorted) // 2]
        for it in items:
            if median_w > 0 and it['w'] > median_w * 1.9:
                it['wide'] = True

    print('[验证码] ddddocr 识别: ' +
          ' '.join(f'{it["name"] or "?"}{"!" if it["wide"] else ""}'
                   f'@{it["cx"]:.0f},{it["cy"]:.0f}' for it in items))
    return items


def resolve_char_names(items, targets):
    """
    确定每个框里到底是什么字。
    ddddocr 免费且对单个字符很准，先跑；只有目标字没认全时才调视觉模型兜底。
    """
    dd = [it['name'] for it in items]
    if all(t in dd for t in targets):
        return dd

    print('[验证码] ddddocr 未认全目标字，调用视觉模型补识别')
    vlm = recognize_many([it['crop'] for it in items])
    merged = []
    for it, d, v in zip(items, dd, vlm):
        if d in targets and not it['wide']:
            merged.append(d)        # ddddocr 直接命中，最可信
        elif v in targets:
            merged.append(v)        # ddddocr 没命中而模型命中：采信，否则永远找不到
        elif d and not it['wide']:
            merged.append(d)
        else:
            merged.append(v or d)
    print('[验证码] 视觉模型识别: ' + ' '.join(x or '?' for x in merged))
    return merged


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
            // 极验 V4 参考图在 <img src>（原图 128~200px），background-image 恒为 none。
            const nineEl = q('.geetest_subitem.geetest_nine');
            const clickEl = q('.geetest_subitem.geetest_click');
            const iconEl = q('.geetest_subitem.geetest_icon');
            const winEl = q('.geetest_window');
            const bgEl = q('.geetest_bg');
            const tipsEl = q('.geetest_text_tips');
            const quesEl = q('.geetest_ques_tips');
            const quesImg = quesEl ? quesEl.querySelector('img') : null;
            const tiles = Array.from(document.querySelectorAll('.geetest_item_wrap'));
            return {
                nine: !!nineEl, click: !!clickEl, icon: !!iconEl,
                nineRect: rect(nineEl), clickRect: rect(clickEl),
                winRect: rect(winEl), bgRect: rect(bgEl),
                tileCount: tiles.length,
                tips: tipsEl ? (tipsEl.innerText || '').trim() : '',
                quesBg: bgOf(quesEl),
                quesSrc: quesImg ? (quesImg.currentSrc || quesImg.src || '') : '',
                quesImgOk: !!(quesImg && quesImg.complete && quesImg.naturalWidth > 4),
                quesRect: rect(quesEl),
                hasQues: !!quesEl,
                commit: !!q('.geetest_commit'),
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


def refresh_captcha(page):
    """点极验的刷新按钮换一道题（识别不确定时比硬猜划算）"""
    try:
        btn = page.locator('.geetest_refresh').first
        if btn.count() and btn.is_visible():
            btn.click(timeout=2000)
            print('[验证码] 已刷新换题')
            page.wait_for_timeout(1300)
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------- 文本匹配

def parse_count(tips):
    """从「选 3 个符合右图的图片」里解析出需要选几个"""
    m = re.search(r'(\d+)\s*个', tips or '')
    if m:
        n = int(m.group(1))
        if 1 <= n <= 9:
            return n
    return 3


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
    raw = vlm_chat(
        '第一张图是参考物体，第二张图是九宫格中的一个格子。\n'
        '判断第二张图里是否存在和第一张图相同或同类的物体。\n'
        '只回答一个字：是 或 否。不要解释。', [ref_img, cell_img])
    if not raw:
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
        raw = vlm_chat(
            '这是九宫格验证码里的一个格子。用 2-6 个汉字描述格子里最主要的东西：'
            '颜色 + 物体，例如「红色苹果」「蓝色雨伞」。'
            '如果画面是空的或只有底色，只回答：空白。'
            '不要解释，不要标点。', [img])
        return i, (clean_name(raw) or '未知')

    with ThreadPoolExecutor(max_workers=3) as pool:
        for i, name in pool.map(worker, range(len(cells))):
            results[i] = name
    return results


def load_ref_image(st, page, max_wait=3):
    """
    取参考图（题目图），并等它从占位符变成真正的图。
    占位状态下 VLM 会读到随机字母（yqkx/jfdl），必须跳过。
    返回图片字节，失败返回 None。
    """
    ref = None
    for _ in range(max_wait):
        src = st.get('quesSrc') or st.get('quesBg')
        # img 存在但还没加载完：此时 src 指向的是随机字母占位图，必须等
        if src and not st.get('quesImgOk'):
            page.wait_for_timeout(900)
            st = captcha_state(page)
            src = st.get('quesSrc') or st.get('quesBg')
        ref = fetch_image(src)
        if not ref:
            ref = shot_el(page, '.geetest_ques_tips')
        if ref:
            return ref
        page.wait_for_timeout(700)
        st = captcha_state(page)
    return ref


# ---------------------------------------------------------------- 九宫格

def solve_nine(page, st):
    """九宫格：命名参考图 + 命名 9 格 + 文本匹配 + 按元素点击（零坐标）"""
    need = parse_count(st.get('tips'))
    print(f'[验证码] 九宫格，需选 {need} 个 | 原始题目: {st.get("tips")!r}')

    # 1) 参考图标：等加载 + 优先下载原图
    ref, ref_name = None, ''
    for attempt in range(3):
        raw_ref = load_ref_image(st, page)
        if not raw_ref:
            print('[验证码] 未取到参考图，放弃本轮')
            return False
        ref = upscale_png(raw_ref, 4, 640)
        ref_name = clean_name(vlm_chat(
            '这是验证码的参考图标。用 2-6 个汉字描述它：颜色 + 物体，'
            '例如「红色苹果」「蓝色雨伞」；抽象图形就描述颜色和形状，例如「黑色圆形」。'
            '只回答这几个字，不要解释、不要标点，不要回答「未知」。', [ref]))
        # 纯字母/数字说明还是占位符状态，等一下再读
        if ref_name and ref_name not in EMPTY_WORDS and re.search(r'[\u4e00-\u9fff]', ref_name):
            break
        print(f'[验证码] 参考图疑似未加载完成: {ref_name!r}，等待 ({attempt + 1}/3)')
        page.wait_for_timeout(1200)
        st = captcha_state(page)
        if not st.get('quesImgOk'):
            # 图压根没加载出来，点刷新换一张
            refresh_captcha(page)
            st = captcha_state(page)

    if not ref_name or ref_name in EMPTY_WORDS:
        print(f'[验证码] 参考图无法识别: {ref_name!r}，刷新换题')
        return None if refresh_captcha(page) else False
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

    # 4) 名称匹配。阈值取 0.75：描述式命名（「红色苹果」vs「青色苹果」）相似度有 0.6，
    #    阈值太低会把不同颜色的同类物体也选进来，宁可留给下一步的逐格比对
    scored = score_cells(ref_name, names)
    idx = [i for s, i, _ in scored if s >= 0.75][:need]
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


# ---------------------------------------------------------------- 文字点选

def recognize_ref_chars(st, page):
    """从参考图识别要依次点击的汉字。
    ikuuu 的 tips 只是『请在下图依次点击』，不含具体字；
    真正的字在 geetest_ques_tips 的 <img> 里，如『炒肝片』。
    注意：ddddocr 的检测器是场景文字模型，对白底提示图无效，这里只能靠视觉模型。"""
    for attempt in range(3):
        ref = load_ref_image(st, page)
        if not ref:
            print('[验证码] 参考图（待点字）获取失败')
            return []
        ref_up = upscale_png(ref, 4, 640)
        raw = vlm_chat(
            '这是极验文字点选验证码的「提示图」，图里写着 2~4 个汉字，'
            '要求按从左到右的顺序在下方大图里依次点击这些字。\n'
            '只输出图中这些汉字本身，不要解释、不要标点、不要引号。'
            '例如图里是「炒肝片」就只回答：炒肝片',
            [ref_up])
        chars = re.findall(r'[\u4e00-\u9fff]', raw or '')
        chars = [c for c in chars if c not in '图中是这的是字依次点击请按顺序汉字如下']
        # 去重保序（模型复读时会产生上百个重复字）
        seen, uniq = set(), []
        for c in chars:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        if uniq:
            print(f'[验证码] 参考图识别待点字: {uniq} (原始: {raw[:40]!r})')
            return uniq[:6]
        # 全是字母/空 -> 还是占位符，等图加载
        print(f'[验证码] 参考图疑似占位符: {(raw or "")[:30]!r}，等待加载 ({attempt + 1}/3)')
        page.wait_for_timeout(1200)
        st = captcha_state(page)
    return []


def solve_click(page, st):
    """文字点选：参考图读字 -> OCR 定位图内所有字 -> 按字匹配 -> 点框中心。
    返回值：True 已提交 / False 硬失败 / None 已刷新换题（不算失败）"""
    box = st.get('winRect') or st.get('clickRect') or st.get('bgRect')
    if not box or box.get('w', 0) < 40:
        print('[验证码] 未取到点选区域')
        return False

    # 1) 要点的字来自参考图（不是 tips 文字）
    chars = recognize_ref_chars(st, page)
    if not chars:
        print('[验证码] 参考图始终无法识别，刷新换题')
        return None if refresh_captcha(page) else False

    # 2) 大图截图
    img = shot_el(page, '.geetest_window') or shot_el(page, '.geetest_bg')
    if not img:
        print('[验证码] 点选区域截图失败')
        return False

    # 3) 检测 + 逐字识别，拿到「图上有什么字、在哪」
    items = detect_chars(img, box['w'], box['h'], scale=2)
    if not items:
        print('[验证码] 图内未检测到文字，刷新换题')
        return None if refresh_captcha(page) else False

    names = resolve_char_names(items, chars)

    # 4) 按题目顺序匹配框（一个字只用一次）
    used, plan = set(), []
    for ch in chars:
        hit = None
        for i, n in enumerate(names):
            if i not in used and n == ch:
                hit = i
                break
        if hit is None:
            print(f'[验证码] 图内没有「{ch}」，判定参考图识别有误，刷新换题')
            return None if refresh_captcha(page) else False
        used.add(hit)
        plan.append((ch, items[hit]['cx'], items[hit]['cy']))

    # 5) 点击（坐标全部来自 OCR，模型不参与定位）
    for ch, cx, cy in plan:
        page.mouse.click(box['x'] + cx, box['y'] + cy)
        print(f'  点击「{ch}」@ ({box["x"] + cx:.0f},{box["y"] + cy:.0f})')
        time.sleep(random.uniform(0.35, 0.7))

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

    if ok is None:
        return None          # 已刷新换题，本轮不计失败
    if not ok:
        return False

    # 等极验回调结果
    for _ in range(12):
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
        # fails 计「答错」，rounds 是总轮数上限；主动刷新换题不占失败次数
        if not ok:
            fails, rounds = 0, 0
            while fails < MAX_CAPTCHA_RETRY and rounds < MAX_CAPTCHA_RETRY + MAX_REFRESH:
                rounds += 1
                print(f'[验证码] 视觉识别 第 {rounds} 轮（已答错 {fails} 次）...')
                res = vlm_solve(page, domain)
                if res is True:
                    ok = True
                    break
                if res is None:
                    # 刷新换题后等新题出现
                    page.wait_for_timeout(900)
                    if not detect_kind(captcha_state(page)):
                        click_first(page, ['.geetest_btn_click', '[class*="geetest_btn"]'])
                        page.wait_for_timeout(1200)
                    continue
                fails += 1
                # 极验答错后会自动换新题，这里只等新题出现并重新判型；
                # 只有整个面板都消失了才需要重新点验证按钮。
                page.wait_for_timeout(1500)
                if not detect_kind(captcha_state(page)):
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
    print(f'文字检测: {"ddddocr" if ddddocr else "未安装（点选题成功率低）"}')

    for domain in DOMAINS:
        for attempt in range(1, DOMAIN_RETRY + 1):
            try:
                ok, msg = try_domain(domain)
            except Exception as e:
                ok, msg = False, f'异常: {type(e).__name__}: {e}'
            if ok:
                print(f'✅ 签到成功 [{domain}]: {msg}')
                push('✅ ikuuu 签到成功', f'域名: {domain}\n{msg}\n邮箱: {EMAIL}')
                return True
            print(f'[{domain}] 第 {attempt} 次失败: {msg}')
            if attempt < DOMAIN_RETRY:
                time.sleep(3)

    print('[ERROR] 所有域名均签到失败')
    push('❌ ikuuu 签到失败',
         f'所有域名尝试完毕仍失败。\n账号: {EMAIL}\n'
         f'请到 Actions 下载 captcha_*.png 截图查看验证码类型。')
    return False


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
