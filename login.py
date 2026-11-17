"""Open Barchart support discussions, log in, and paste the next generated topic."""
# NOTE: Hình dung một người ngồi trước Chrome và làm hộ bạn bốn việc:
# mở diễn đàn Barchart, đăng nhập Google, nhờ AI trên máy viết một bài coupon,
# rồi dán bài vào form. Cửa sổ Chrome hiện ra thật, bạn nhìn được từng bước.
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pyotp
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# NOTE: Mọi file (sổ bài, ảnh, script captcha) đều tính từ thư mục chứa login.py.
# Chạy lệnh từ thư mục khác vẫn tìm đúng chỗ, như lấy đồ trong nhà chứ không phải trong túi áo.
ROOT = Path(__file__).resolve().parent
DISCUSSIONS = "https://help.barchart.com/support/discussions"
LOGIN = "https://help.barchart.com/support/login"
# NOTE: Ô mã 6 số của Google đổi tên tùy trang. Đây là danh sách biển tên có thể gặp,
# chương trình thử từ trái sang phải cho tới khi thấy một ô.
OTP = 'input[autocomplete="one-time-code"], input[name*="otp" i], input[id*="otp" i], input[name="code"]'
# NOTE: LM Studio là bộ AI chạy sẵn trên máy này, cổng 1234.
# Câu hỏi tiêu đề và nội dung bài đi tới đó, model tên meta-llama-3.1-8b-instruct.
LM_CHAT = "http://localhost:1234/api/v1/chat"
LM_MODEL = "meta-llama-3.1-8b-instruct"
TOPICS = ROOT / "topics.json"
POSTED_LINKS = ROOT / "posted-links.json"
RECAPTCHA_SOLVER = ROOT / "solve-recaptcha.mjs"
CDP_PORT = 0

# NOTE: Tờ sự thật duy nhất của chiến dịch. AI chỉ được nhắc những gì có ở đây.
# Chỗ {code} trong câu lệnh sẽ thành BF2026. Đổi deal thì sửa một chỗ này.
CONFIG = {
    "brand": "Peptira",
    "site": "Peptira.com",
    "code": "BF2026",
    "discount": "Up to 50% Off",
    "event": "Black Friday 2026",
    "backupCode": "10OFF",
    "link": "[LINK]",
}


def configure_state() -> None:
    # NOTE: Mỗi tài khoản một ngăn kéo riêng, tên ngăn là BARCHART_STATE_NAME.
    # Có tên thì topics.json và posted-links.json nằm trong state/<tên>.
    # Trống tên thì cả máy dùng chung hai file ở thư mục gốc.
    global TOPICS, POSTED_LINKS

    name = os.environ.get("BARCHART_STATE_NAME", "").strip()
    if not name:
        return
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
    if not safe_name:
        raise RuntimeError("invalid BARCHART_STATE_NAME")
    state_dir = ROOT / "state" / safe_name
    state_dir.mkdir(parents=True, exist_ok=True)
    TOPICS = state_dir / "topics.json"
    POSTED_LINKS = state_dir / "posted-links.json"
    print("account state:", state_dir.relative_to(ROOT), flush=True)

# NOTE: Một bài là một ô trong bảng: 12 cụm từ khóa nhân 4 cách viết.
# Hết ô thì script báo không còn việc, như tô hết ô trong tờ bài tập.
KEYWORDS = [
    "Peptira coupon code",
    "Peptira promo code",
    "Peptira discount code",
    "Peptira Black Friday coupon",
    "Peptira Black Friday sale",
    "Peptira Black Friday deal",
    "Peptira voucher code",
    "Peptira coupon",
    "Peptira promo",
    "Peptira discount",
    "Peptira deals",
    "Peptira code",
]

ANGLES = [
    "how-to: step-by-step guide to applying the code at checkout",
    "deal summary: short overview of the deal with a details list (code, discount, where it works, status)",
    "FAQ: 3-4 short questions and answers about the code",
    "troubleshooting: what to check if the code does not apply, including the backup code",
]

# NOTE: Luật chơi đưa cho AI trước khi nó đặt tiêu đề.
# Giống đề bài: được dùng fact nào, phải có keyword và [BF2026], nộp đúng một dòng.
TITLE_SYSTEM = """You write titles for coupon/deal forum topics.

Facts you may use (do not add any others):
- Store: {brand} ({site}), sells research peptides, research use only.
- Coupon code: {code}
- Discount: {discount}
- Event: {event}

Rules:
- Must contain the target keyword exactly, the code in square brackets [{code}], "{discount}" (or "Save Up to 50%"), and "Black Friday".
- Aim for 60-100 characters. No emojis, no ALL CAPS words except the code.
- Must be clearly different in wording and word order from every title in the "already used" list.
- Output exactly ONE title on ONE line.
- Do not output JSON, labels, quotation marks, markdown, or explanations.
- Before answering, verify the literal text [{code}] is present."""


# NOTE: Luật chơi cho thân bài. AI viết 120-180 từ theo đúng "angle" đã chọn,
# nhắc mã vài lần, chèn dòng link, và kết bằng câu research-use-only.
MESSAGE_SYSTEM = """You write the message body for one coupon/deal forum topic.

Facts you may use (do not add any others):
- Store: {brand} ({site}), sells research peptides, research use only.
- Coupon code: {code}
- Discount: {discount}
- Event: {event}
- Code status: active, confirmed by {brand}.
- Backup code if the main one does not apply: {backupCode}
- Link for the full list of codes: {link}

Rules:
- English, 120-180 words, plain text (no markdown, no HTML), short paragraphs or simple numbered/dashed lists.
- Use the target keyword in the first sentence and once more later. Mention the code {code} 2-3 times.
- Stay fully consistent with the supplied final title, target keyword, and angle.
- Follow the requested angle.
- Include the link line once, near the end: "Full list of verified {brand} codes: {link}"
- End with: "Note: {brand} products are sold for research use only."
- Do NOT invent anything: no prices, expiry dates, product names, exclusions, stacking rules, shipping offers, testimonials or personal experience ("I saved...", "I tested..."). No health, medical or results claims.
- Output only the message body. Do not output the title, JSON, labels, quotation marks, markdown fences, or explanations."""


def load_env(path: Path) -> None:
    # NOTE: .env là tờ giấy khóa cạnh script: email, mật khẩu, chìa mã 6 số.
    # Mỗi dòng TÊN=giá trị được đổ vào môi trường. Tên đã có sẵn thì giữ nguyên,
    # nên giá trị bạn gõ trên lệnh thắng tờ giấy.
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def totp_code(secret: str) -> str:
    # NOTE: Mã 6 số đổi mỗi 30 giây. Điện thoại và máy này cầm cùng một chìa,
    # nên cùng một lúc ra cùng một số, như hai đồng hồ chỉnh cùng giờ.
    # Khoảng trắng trong chìa bị xóa trước khi tính.
    clean = re.sub(r"\s+", "", secret).upper()
    return pyotp.TOTP(clean).now()


def fill_prompt(text: str) -> str:
    # NOTE: Điền chỗ trống. {code} thành BF2026, {brand} thành Peptira, và tương tự.
    # Chỗ trống lạ, không có trong CONFIG, được giữ nguyên từng chữ.
    return re.sub(r"\{(\w+)\}", lambda m: str(CONFIG.get(m.group(1), m.group(0))), text)


def parse_topic(text: str) -> dict:
    # NOTE: Bản cũ của AI hay trả một cục chữ có JSON nằm giữa lời thừa.
    # Hàm moi cặp ngoặc { } ngoài cùng. JSON hỏng thì cắt "title" và "message" bằng tay.
    # Đường viết bài hiện tại đi thẳng chữ thường. Hàm này còn để lệnh --check tự thử.
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError("no JSON: " + text[:200])
    raw = text[start : end + 1]
    try:
        topic = json.loads(raw, strict=False)
    except json.JSONDecodeError:
        title_match = re.search(r'"title"\s*:\s*"((?:\\.|[^"\\])*)"', raw, re.S)
        message_match = re.search(r'"message"\s*:\s*(.*?)\s*}\s*$', raw, re.S)
        if not title_match or not message_match:
            raise
        message = message_match.group(1).strip()
        if message.startswith('"') and message.endswith('"'):
            message = message[1:-1]
        topic = {
            "title": bytes(title_match.group(1), "utf-8").decode("unicode_escape"),
            "message": message.replace(r'\n', "\n").replace(r'\"', '"').strip(),
        }
    title, message = topic.get("title", ""), topic.get("message", "")
    code = CONFIG["code"]
    if f"[{code}]" not in title and code in title:
        title = title.replace(code, f"[{code}]", 1)
    if f"[{CONFIG['code']}]" not in title:
        raise RuntimeError("Title thiếu mã: " + title)
    if not message:
        raise RuntimeError("missing message")
    return {"title": title, "message": message}


def _check() -> None:
    # NOTE: Bài kiểm tra nhỏ, chạy bằng python login.py --check rồi thoát.
    # Câu hỏi: chìa mẫu có ra đúng 6 chữ số, và hàm moi JSON có cắt đúng bài mẫu.
    code = totp_code("JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP")
    assert len(code) == 6 and code.isdigit(), code
    sample = parse_topic('note {"title": "x [BF2026] y", "message": "z"} trailing')
    assert sample == {"title": "x [BF2026] y", "message": "z"}, sample
    print("totp ok")


def load_topics() -> list:
    # NOTE: Đọc sổ bài đã viết. File chưa có nghĩa là sổ trắng, trả về danh sách rỗng.
    if not TOPICS.exists():
        return []
    return json.loads(TOPICS.read_text(encoding="utf-8"))


def load_posted_links() -> list:
    # NOTE: Đọc sổ link đã đăng. Đây là thứ đếm tiến độ khi đăng hàng loạt.
    if not POSTED_LINKS.exists():
        return []
    return json.loads(POSTED_LINKS.read_text(encoding="utf-8"))


def save_posted_link(title: str, url: str) -> list:
    # NOTE: Ghi một link vào sổ. Cùng một URL đã có thì bỏ qua, khỏi chép hai lần.
    links = load_posted_links()
    if not any(item.get("url") == url for item in links):
        links.append({"title": title, "url": url})
        POSTED_LINKS.write_text(
            json.dumps(links, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    return links


def next_job(topics: list):
    # NOTE: Tìm ô kế tiếp trong bảng 12 cụm × 4 cách viết.
    # Mỗi ô đã làm mang dấu "keyword|angle". Ô đầu chưa có dấu chính là việc kế.
    done = {f"{item['keyword']}|{item['angle']}" for item in topics}
    for keyword in KEYWORDS:
        for angle in ANGLES:
            if f"{keyword}|{angle}" not in done:
                return keyword, angle
    return None


def call_lm(system_prompt: str, prompt: str, max_tokens: int) -> str:
    # NOTE: Đưa luật chơi và đề bài sang LM Studio, đợi tối đa 3 phút.
    # Câu trả lời là các mảnh loại "message" ghép lại. Chuỗi rỗng được coi là lỗi,
    # để khỏi đăng một bài trắng. store false nghĩa là phía AI không cất cuộc hội thoại.
    body = json.dumps(
        {
            "model": LM_MODEL,
            "system_prompt": fill_prompt(system_prompt),
            "input": prompt,
            "max_output_tokens": max_tokens,
            "store": False,
        }
    ).encode()
    req = urllib.request.Request(
        LM_CHAT, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as res:
            data = json.loads(res.read().decode())
    except urllib.error.HTTPError as err:
        raise RuntimeError(f"HTTP {err.code}: {err.read().decode()[:300]}") from err
    text = "".join(
        item.get("content", "")
        for item in data.get("output", [])
        if item.get("type") == "message"
    )
    if not text.strip():
        raise RuntimeError("LM Studio returned empty output")
    return text.strip()


def clean_plain_output(text: str) -> str:
    # NOTE: AI thỉnh thoảng bọc câu trong hàng rào ``` hoặc dấu ngoặc kép.
    # Gỡ lớp vỏ đó để form nhận đoạn văn thường.
    text = re.sub(r"^```(?:text)?\s*|\s*```$", "", text.strip(), flags=re.I)
    return text.strip().strip('"').strip()


def generate_topic(keyword: str, angle: str, used_titles: list) -> dict:
    # NOTE: Viết một bài bằng hai lượt hỏi. Lượt một ra tiêu đề. Lượt hai viết thân bài
    # khi đã nhìn thấy tiêu đề vừa chốt, để hai phần khớp nhau.
    # Tiêu đề thiếu keyword, thiếu [mã], hoặc thiếu chữ Black Friday thì vứt cả bài.
    used = "\n".join(f"- {title}" for title in used_titles) or "(none)"
    title_prompt = (
        f"Target keyword: {keyword}\n"
        f"Angle for context: {angle}\n"
        f"Already used titles (do not repeat or closely imitate):\n{used}"
    )
    title = clean_plain_output(call_lm(TITLE_SYSTEM, title_prompt, 160))
    title = " ".join(title.splitlines()).strip()
    code = CONFIG["code"]
    if f"[{code}]" not in title and code in title:
        title = title.replace(code, f"[{code}]", 1)
    if keyword.casefold() not in title.casefold():
        raise RuntimeError("Title thiếu keyword: " + title)
    if f"[{code}]" not in title:
        raise RuntimeError("Title thiếu mã: " + title)
    if "Black Friday" not in title:
        raise RuntimeError("Title thiếu Black Friday: " + title)

    message_prompt = (
        f"Final title: {title}\n"
        f"Target keyword: {keyword}\n"
        f"Angle: {angle}\n"
        "Write the matching message now."
    )
    message = clean_plain_output(call_lm(MESSAGE_SYSTEM, message_prompt, 700))
    if not message:
        raise RuntimeError("missing message")
    return {"title": title, "message": message}


def fill_new_topic(page, title: str, message: str) -> None:
    # NOTE: Điền form bài mới. Tiêu đề vào ô input.
    # Ô nội dung có hai đời: ô soạn thảo (bấm vào, chọn hết, gõ đè) hoặc textarea cũ.
    # Tìm ô bên trong đúng form của tiêu đề, kẻo điền nhầm ô tìm kiếm của trang.
    title_box = page.locator(
        "#topic_title, input[name='topic[title]'], input[name='title']"
    ).first
    title_box.wait_for(state="visible", timeout=15000)
    title_box.fill(title)
    form = title_box.locator("xpath=ancestor::form[1]")
    editor = form.locator("[contenteditable='true']")
    if editor.count() and editor.last.is_visible():
        editor.last.click()
        page.keyboard.press("Control+A")
        page.keyboard.insert_text(message)
        return
    form.locator("textarea").first.fill(message)


def choose_forum(page) -> None:
    # NOTE: Trang có vài menu xổ xuống. Menu nào có mục chứa chữ "trick"
    # thì chọn mục đó (Tips and Tricks). Thiếu mục này thì dừng, khỏi đăng nhầm diễn đàn.
    selects = page.locator("select")
    for i in range(selects.count()):
        select = selects.nth(i)
        labels = [text.strip() for text in select.locator("option").all_inner_texts()]
        match = next((text for text in labels if "trick" in text.lower()), "")
        if not match:
            continue
        select.select_option(label=match)
        print("forum:", match, flush=True)
        return
    raise RuntimeError("no Tips and Tricks option")


def available_local_port() -> int:
    # NOTE: Xin hệ điều hành một cổng mạng đang trống trên máy mình.
    # Chrome sắp mở một cửa sổ nói chuyện ở cổng đó, để script captcha điều khiển đúng cửa sổ.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def recaptcha_is_checked(page) -> bool:
    # NOTE: Captcha xong khi một trong hai dấu xuất hiện:
    # ô ẩn đã có chuỗi Google trả về, hoặc ô tick trong khung Google báo đã chọn.
    response = page.locator('textarea[name="g-recaptcha-response"]')
    if response.count() and response.first.input_value().strip():
        return True
    for frame in page.frames:
        anchor = frame.locator("#recaptcha-anchor")
        if anchor.count() and anchor.first.get_attribute("aria-checked") == "true":
            return True
    return False


def wait_for_manual_recaptcha(page, timeout_ms: int = 600000) -> None:
    # NOTE: Máy giải captcha bó tay thì tới lượt người. Script đứng nhìn tối đa 10 phút,
    # mỗi giây hỏi một lần. Thấy đã tick thì quay lại để bước Save tự chạy.
    print(
        "Solver unavailable. Complete reCAPTCHA manually; Save will be clicked automatically.",
        flush=True,
    )
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if recaptcha_is_checked(page):
            print("reCAPTCHA completed manually.", flush=True)
            return
        page.wait_for_timeout(1000)
    raise RuntimeError("manual reCAPTCHA timed out after 10 minutes")


def reload_recaptcha_challenge(page) -> bool:
    # NOTE: Câu captcha sai thì bấm nút tải lại trong khung Google, như lật sang câu khác.
    # Nút nằm trong iframe, nên phải mở từng khung con, trang ngoài không có nút đó.
    for frame in page.frames:
        try:
            reload_button = frame.locator("#recaptcha-reload-button")
            if reload_button.count() and reload_button.first.is_visible():
                reload_button.first.click()
                page.wait_for_timeout(2500)
                print("reCAPTCHA challenge reloaded.", flush=True)
                return True
        except Exception:
            continue
    print("reCAPTCHA reload button not found.", flush=True)
    return False


def solve_recaptcha(page) -> None:
    # NOTE: Gọi solve-recaptcha.mjs và đưa hai manh mối: số cổng Chrome, và URL trang hiện tại.
    # Script đó nghe audio captcha rồi điền đáp án. Tối đa 3 lần, mỗi lần tối đa 3 phút.
    # Hết lượt thì nhường người tick tay.
    if not RECAPTCHA_SOLVER.exists():
        raise RuntimeError(f"missing {RECAPTCHA_SOLVER.name}")
    if not CDP_PORT:
        raise RuntimeError("Chrome CDP port is not configured")

    page.wait_for_timeout(1000)
    last_error = "unknown solver error"
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        print(f"reCAPTCHA solver attempt {attempt}/{max_attempts}", flush=True)
        try:
            result = subprocess.run(
                [
                    "node",
                    str(RECAPTCHA_SOLVER),
                    str(CDP_PORT),
                    page.url,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            if result.stdout.strip():
                print(result.stdout.strip(), flush=True)
            if result.returncode == 0:
                return
            last_error = result.stderr.strip() or "unknown solver error"
        except subprocess.TimeoutExpired:
            last_error = "solver timed out after 180 seconds"

        print(f"reCAPTCHA solver failed ({attempt}/{max_attempts}): {last_error}", flush=True)
        if attempt < max_attempts and not reload_recaptcha_challenge(page):
            break

    print("Automatic reCAPTCHA attempts exhausted.", flush=True)
    wait_for_manual_recaptcha(page)


def attach_image(page) -> None:
    # NOTE: Gắn một ảnh coupon vào bài. Ô chọn file đang ẩn thì bấm chữ Attach cho nó hiện.
    # Ảnh vào form xong mới gọi captcha, vì khung captcha thường chỉ xuất hiện sau bước này.
    image = ROOT / "peptira-coupon-code-bf2026-up-to-50-off-with-black-friday-sale-prices.jpg"
    if not image.exists():
        raise RuntimeError(f"missing {image.name}")
    files = page.locator("input[type='file']")
    if files.count() == 0:
        page.get_by_text(re.compile("attach", re.I)).first.click()
        files = page.locator("input[type='file']")
    files.first.set_input_files(str(image))
    print("attached:", image.name, flush=True)
    solve_recaptcha(page)


def paste_saved(page) -> None:
    # NOTE: Đường --fill. Lấy bài cuối cùng trong sổ, dán lại, chọn diễn đàn, gắn ảnh, bấm Save.
    # AI không được gọi. Dùng khi bài đã viết và bạn muốn đăng đúng bài đó.
    topics = load_topics()
    if not topics:
        print("no saved topic", flush=True)
        return
    last = topics[-1]
    fill_new_topic(page, last["title"], last["message"])
    choose_forum(page)
    attach_image(page)
    print("filled:", last["title"], flush=True)
    page.locator('input[type="submit"][value="Save"]').click()
    page.wait_for_load_state("domcontentloaded")
    print("saved:", page.url, flush=True)


def paste_next_topic(page) -> str | None:
    # NOTE: Đường một bài, không có --batch và không có --fill.
    # Viết bài mới, dán vào form đang mở, ghi sổ topics.json, rồi dừng để bạn đọc.
    # Nút Save ở đường này do bạn bấm. Hỏi AI tối đa 3 lần, mỗi lần nghỉ lâu hơn lần trước.
    topics = load_topics()
    job = next_job(topics)
    if job is None:
        print(f"No topics left ({len(topics)})")
        return None
    keyword, angle = job
    used = [item["title"] for item in topics]
    topic = None
    for attempt in range(1, 4):
        try:
            topic = generate_topic(keyword, angle, used)
            break
        except Exception as err:
            print(f"Lỗi ({attempt}/3) {keyword}: {err}")
            time.sleep(3 * attempt)
    if topic is None:
        return None
    fill_new_topic(page, topic["title"], topic["message"])
    topics.append({"keyword": keyword, "angle": angle, **topic})
    TOPICS.write_text(json.dumps(topics, indent=2), encoding="utf-8")
    print(f"[{len(topics)}] {topic['title']}")
    return topic["title"]


def open_new_topic(page) -> None:
    # NOTE: Bấm nút tạo bài trên trang thảo luận.
    # Đang đứng ở trang khác thì quay về danh sách thảo luận trước, rồi mới bấm.
    if page.url.rstrip("/") != DISCUSSIONS.rstrip("/"):
        page.goto(DISCUSSIONS, wait_until="domcontentloaded")
    topic = page.locator('div.start-topic a[href="/support/discussions/topics/new"]')
    topic.first.wait_for(timeout=15000)
    topic.first.click()
    page.wait_for_load_state("domcontentloaded")
    print("open:", page.url, flush=True)


def collect_topic_link(page, title: str) -> str:
    # NOTE: Sau khi Save, bài mới hiện trên danh sách hơi trễ.
    # Tải lại trang tối đa 7 lần và tìm đường link có đúng nguyên văn tiêu đề.
    # Link kết thúc bằng /topics/new là nút tạo bài, bỏ qua.
    if page.url.rstrip("/") != DISCUSSIONS.rstrip("/"):
        page.goto(DISCUSSIONS, wait_until="domcontentloaded")
    for attempt in range(1, 8):
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        url = page.locator('a[href*="/support/discussions/topics/"]').evaluate_all(
            """(links, expected) => {
              const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
              const match = links.find((link) => clean(link.textContent) === clean(expected));
              return match ? match.href : null;
            }""",
            title,
        )
        if url and not url.endswith("/topics/new"):
            print("collected:", url, flush=True)
            return url
        print(f"waiting for My Topics refresh ({attempt}/7)", flush=True)
        page.wait_for_timeout(1500)
    raise RuntimeError(f"topic link not found in My Topics: {title}")


def generate_next_topic(topics: list) -> tuple[str, str, dict]:
    # NOTE: Xin AI viết ô kế tiếp, tối đa 8 lần nếu tiêu đề hỏng luật.
    # Nghỉ 3 giây, rồi 6, rồi 9... và dừng ở 12 giây, cho máy AI kịp thở giữa các lần.
    job = next_job(topics)
    if job is None:
        raise RuntimeError(f"No topics left ({len(topics)})")
    keyword, angle = job
    used = [item["title"] for item in topics]
    max_attempts = 8
    for attempt in range(1, max_attempts + 1):
        try:
            return keyword, angle, generate_topic(keyword, angle, used)
        except Exception as err:
            print(f"Lỗi ({attempt}/{max_attempts}) {keyword}: {err}", flush=True)
            time.sleep(min(3 * attempt, 12))
    raise RuntimeError(f"LM Studio could not generate topic: {keyword}")


def post_one_generated_topic(page) -> tuple[str, str]:
    # NOTE: Một vòng đăng trọn vẹn: viết, mở form, điền, chọn diễn đàn, gắn ảnh,
    # giải captcha, bấm Save, nhặt link bài vừa hiện, rồi ghi cả hai sổ.
    # Sổ topics chỉ nhận bài sau khi đã có link, nên bài đăng hỏng không bị tính là đã xong.
    topics = load_topics()
    keyword, angle, topic = generate_next_topic(topics)
    open_new_topic(page)
    fill_new_topic(page, topic["title"], topic["message"])
    choose_forum(page)
    attach_image(page)
    print("filled:", topic["title"], flush=True)
    page.locator('input[type="submit"][value="Save"]').click()
    page.wait_for_load_state("domcontentloaded")
    print("saved:", page.url, flush=True)
    url = collect_topic_link(page, topic["title"])
    topics.append({"keyword": keyword, "angle": angle, **topic})
    TOPICS.write_text(
        json.dumps(topics, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    save_posted_link(topic["title"], url)
    return topic["title"], url


def run_batch(page, target: int) -> None:
    # NOTE: Đăng cho đủ target link rồi đăng xuất.
    # Đếm bằng sổ link đã lưu, nên chạy lại giữa chừng sẽ nối tiếp chứ không đếm lại từ 1.
    # Sổ link đang trống mà sổ bài còn bài cuối: nhặt link bài đó trước, khỏi đăng trùng.
    # Giữa hai bài nghỉ 5 giây.
    links = load_posted_links()
    if not links:
        topics = load_topics()
        if topics:
            latest = topics[-1]
            url = collect_topic_link(page, latest["title"])
            links = save_posted_link(latest["title"], url)
            print(f"batch progress: {len(links)}/{target}", flush=True)
    while len(links) < target:
        post_one_generated_topic(page)
        links = load_posted_links()
        print(f"batch progress: {len(links)}/{target}", flush=True)
        if len(links) < target:
            page.wait_for_timeout(5000)
    logout = page.locator('a[href*="logout"]').first
    logout.wait_for(state="visible", timeout=15000)
    logout.click()
    page.wait_for_load_state("domcontentloaded")
    print(f"logged out after {len(links)} saved links", flush=True)


def batch_target() -> int | None:
    # NOTE: python login.py --batch 3 là đăng 3 bài. --batch thiếu số thì lấy 5.
    # Không có cờ --batch thì hàm trả về trống, và chương trình chỉ chuẩn bị một bài.
    if "--batch" not in sys.argv:
        return None
    index = sys.argv.index("--batch")
    if index + 1 < len(sys.argv) and sys.argv[index + 1].isdigit():
        return int(sys.argv[index + 1])
    return 5


def logged_in(page) -> bool:
    # NOTE: Dấu đã vào nhà là trang có link logout. Trang đã đóng thì coi như chưa đăng nhập.
    if page.is_closed():
        return False
    try:
        return page.locator('a[href*="logout"]').count() > 0
    except Exception:
        return False


def fill_otp(page, secret: str) -> None:
    # NOTE: Hàm dự phòng cho ô mã 6 số. Thấy ô trong 4 giây thì điền và bấm Enter.
    # Hết 4 giây mà ô không hiện thì bỏ qua, vì Google không hỏi mã ở mọi lần vào.
    # Đường login_google tự điền mã, nên hàm này đứng ngoài cuộc.
    if not secret:
        return
    box = page.locator(OTP).first
    try:
        box.wait_for(state="visible", timeout=4000)
    except PWTimeout:
        return
    box.fill(totp_code(secret))
    page.keyboard.press("Enter")


def first_visible(page, selector: str, tries: int = 40):
    # NOTE: Ô mật khẩu của Google nằm trong khung nhúng, không nằm trên trang cha.
    # Cứ nửa giây nhìn một vòng mọi khung. Khoảng 20 giây. Thấy đã login thì dừng tìm.
    for _ in range(tries):
        if page.is_closed():
            return None
        if logged_in(page):
            return None
        for frame in page.frames:
            try:
                box = frame.locator(selector)
                if box.count() and box.first.is_visible():
                    return box.first
            except Exception:
                continue
        try:
            page.wait_for_timeout(500)
        except Exception:
            return None
    return None


def click_visible(page, selector: str) -> bool:
    # NOTE: Bấm phần tử đầu tiên đang hiện, lục trong mọi khung của trang.
    # Không thấy thì trả về false để bước sau còn đường khác, ví dụ bấm Enter.
    for frame in page.frames:
        try:
            box = frame.locator(selector)
            if box.count() and box.first.is_visible():
                box.first.click()
                return True
        except Exception:
            continue
    return False


def recover_open_page(context, page):
    # NOTE: Sau nút Cho phép, Google hay đóng cửa sổ phụ.
    # Trang cũ còn sống thì dùng tiếp. Trang cũ đã đóng thì nhặt tab Barchart còn mở.
    # Hết tab thì dừng hẳn, vì không còn chỗ để bấm.
    if not page.is_closed():
        return page
    pages = [candidate for candidate in context.pages if not candidate.is_closed()]
    if not pages:
        raise RuntimeError("Google OAuth closed and no browser page remains")
    return next(
        (
            candidate
            for candidate in reversed(pages)
            if "help.barchart.com" in candidate.url
        ),
        pages[-1],
    )


def click_google_continue(page, tries: int = 60) -> bool:
    # NOTE: Nút "cho Barchart dùng tài khoản Google" đổi chữ theo ngôn ngữ máy.
    # Nhận Tiếp tục, Continue, Cho phép, Allow, hoặc nút id submit_approve_access.
    # Cửa sổ Google đóng giữa chừng thường là bước này đã xong, nên coi như bấm được.
    selectors = (
        "#submit_approve_access, "
        'button:has-text("Tiếp tục"), [role="button"]:has-text("Tiếp tục"), '
        'button:has-text("Continue"), [role="button"]:has-text("Continue"), '
        'button:has-text("Cho phép"), [role="button"]:has-text("Cho phép"), '
        'button:has-text("Allow"), [role="button"]:has-text("Allow")'
    )
    accepted = {"tiếp tục", "continue", "cho phép", "allow"}
    for _ in range(tries):
        if page.is_closed():
            return True
        for frame in page.frames:
            try:
                candidates = frame.locator(selectors)
                for index in range(candidates.count()):
                    candidate = candidates.nth(index)
                    if not candidate.is_visible() or not candidate.is_enabled():
                        continue
                    text = " ".join(candidate.inner_text().split()).casefold()
                    aria = (candidate.get_attribute("aria-label") or "").strip().casefold()
                    value = (candidate.get_attribute("value") or "").strip().casefold()
                    if candidate.get_attribute("id") == "submit_approve_access" or any(
                        label in accepted for label in (text, aria, value)
                    ):
                        candidate.click()
                        return True
            except Exception:
                continue
        try:
            page.wait_for_timeout(500)
        except Exception:
            return True
    return False


def login_google(page):
    # NOTE: Đăng nhập như người thật, từng bước một vì mỗi ô hiện chậm:
    # bấm Sign in with Google, gõ email, gõ mật khẩu, gõ mã 6 số, rồi bấm cho phép.
    # Email, mật khẩu và chìa mã lấy từ môi trường (file .env đã nạp lúc khởi động).
    context = page.context
    page.goto(LOGIN, wait_until="domcontentloaded")
    google = page.locator("a.btn-google")
    google.wait_for(timeout=15000)
    print("Sign in with Google in the browser window.", flush=True)
    google.click()
    page.wait_for_timeout(2000)
    if "accounts.google." not in page.url and not logged_in(page):
        # HACK: Nút Google của Freshdesk là link kiểu POST. Cú bấm thường đôi khi đứng im.
        # Khi đó tạo một form tạm, chép mã authenticity_token của trang, rồi gửi form đó.
        page.evaluate(
            """() => {
              const a = document.querySelector('a.btn-google');
              const token = document.querySelector('input[name="authenticity_token"]');
              const form = document.createElement('form');
              form.method = 'POST';
              form.action = a.href;
              if (token) form.appendChild(token.cloneNode());
              document.body.appendChild(form);
              form.submit();
            }"""
        )
    try:
        page.wait_for_url("**accounts.google.**", timeout=20000)
    except PWTimeout:
        return page
    email = os.environ.get("BARCHART_EMAIL", "")
    box = None
    for _ in range(40):
        for frame in page.frames:
            candidate = frame.locator("#identifierId, input[name='identifier']")
            if candidate.count():
                box = candidate.first
                break
        if box is not None:
            break
        page.wait_for_timeout(500)
    if email and box is not None:
        box.fill(email)
        for frame in page.frames:
            nxt = frame.locator("#identifierNext")
            if nxt.count():
                nxt.first.click()
                break
        print("Email entered.", flush=True)
    else:
        page.screenshot(path=str(ROOT / "after-login.png"))
        print("no email box", flush=True)
    password = os.environ.get("BARCHART_PASSWORD", "")
    pwd = first_visible(page, 'input[name="Passwd"], input[type="password"]', 30)
    if password and pwd is not None:
        pwd.fill(password)
        click_visible(page, "#passwordNext")
        print("Password entered.", flush=True)
    secret = os.environ.get("BARCHART_TOTP_SECRET", "")
    pin = None
    for _ in range(40):
        if logged_in(page):
            break
        pin = first_visible(
            page,
            '#totpPin, input[name="totpPin"], input[autocomplete="one-time-code"]',
            1,
        )
        if pin is not None:
            break
        # NOTE: data-challengetype="6" là lựa chọn "nhập mã từ ứng dụng" trên màn Google.
        # Bấm nó khi Google đang đưa ra nhiều cách xác minh và ô mã chưa hiện.
        click_visible(page, '[data-challengetype="6"]')
        page.wait_for_timeout(500)
    if secret and pin is not None:
        pin.fill(totp_code(secret))
        if not click_visible(page, "#totpNext"):
            pin.press("Enter")
        print("2FA code entered.", flush=True)
        time.sleep(2)
        page = recover_open_page(context, page)
    clicked_continue = click_google_continue(page)
    if clicked_continue:
        print("Clicked continue.", flush=True)
        time.sleep(2)
        page = recover_open_page(context, page)
    else:
        print("Google continue button not found.", flush=True)
    page = recover_open_page(context, page)
    try:
        # NOTE: Chờ tới 5 phút cho link logout xuất hiện. Hết giờ thì in "login failed"
        # và trả trang về cho main quyết định đóng hay ở lại.
        page.locator('a[href*="logout"]').first.wait_for(timeout=300000)
    except PWTimeout:
        print("login failed", flush=True)
    return page


def main() -> None:
    # NOTE: Người dẫn chuyện. Đọc .env, chọn ngăn tài khoản, mở Chrome có trí nhớ đăng nhập,
    # vào trang thảo luận, đăng nhập nếu còn ở ngoài, rồi đăng một bài hoặc một lô.
    global CDP_PORT

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_env(ROOT / ".env")
    configure_state()
    CDP_PORT = available_local_port()
    if "--fresh" in sys.argv:
        # NOTE: --fresh là một Chrome mới tinh, profile dùng một lần trong thư mục tạm.
        # Thiếu cờ này thì dùng .chrome-profile, nơi cookie đăng nhập được nhớ lần sau.
        profile_dir = Path(tempfile.mkdtemp(prefix=".chrome-profile-fresh-", dir=ROOT))
        print("fresh Chrome profile:", profile_dir.name, flush=True)
    else:
        profile_dir = ROOT / ".chrome-profile"

    with sync_playwright() as p:
        # NOTE: Mở Chrome thật, có cửa sổ. Cổng debug chỉ nghe máy mình (127.0.0.1),
        # script captcha dùng cổng đó để nói với đúng cửa sổ này.
        # Bỏ cờ "đây là trình tự động" mà Playwright hay gắn, sandbox của Chrome vẫn bật.
        ctx = p.chromium.launch_persistent_context(
            str(profile_dir),
            channel="chrome",
            headless=False,
            chromium_sandbox=True,
            ignore_default_args=["--enable-automation", "--no-sandbox"],
            args=[
                f"--remote-debugging-port={CDP_PORT}",
                "--remote-debugging-address=127.0.0.1",
            ],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(DISCUSSIONS, wait_until="domcontentloaded")
        if not logged_in(page):
            page = login_google(page)
        if not logged_in(page):
            print("Still not logged in.", flush=True)
            ctx.close()
            return
        target = batch_target()
        try:
            if target is not None:
                run_batch(page, target)
            else:
                open_new_topic(page)
                if "--fill" in sys.argv:
                    paste_saved(page)
                else:
                    paste_next_topic(page)
        except Exception as err:
            # NOTE: Đăng bài hỏng thì in danh sách ô trên form và lưu nguyên HTML trang
            # vào new-topic.html. Mở file đó là thấy mặt trang lúc lỗi.
            fields = []
            if not page.is_closed():
                fields = page.eval_on_selector_all(
                    "input, textarea, [contenteditable]",
                    "els => els.map(e => [e.id, e.name, e.getAttribute('contenteditable')])",
                )
            print("topic failed:", err)
            print("fields:", fields)
            if not page.is_closed():
                (ROOT / "new-topic.html").write_text(page.content(), encoding="utf-8")
        if os.environ.get("BARCHART_NO_WAIT") == "1":
            # NOTE: BARCHART_NO_WAIT=1 là chế độ chạy xong thì đi: chụp after-login.png rồi đóng.
            page.screenshot(path=str(ROOT / "after-login.png"))
            ctx.close()
            return
        try:
            # NOTE: Có người ngồi ở terminal thì đợi phím Enter mới đóng Chrome.
            # Chạy không có terminal (tác vụ nền) thì giữ cửa sổ thêm 10 phút để kịp nhìn.
            if sys.stdin.isatty():
                input("Run finished. Press Enter to close.")
            else:
                page.wait_for_timeout(600000)
        except EOFError:
            page.wait_for_timeout(600000)
        ctx.close()


if __name__ == "__main__":
    # NOTE: --check chỉ chạy bài tự kiểm tra rồi thoát. Mọi lệnh khác đi vào main.
    if "--check" in sys.argv:
        _check()
    else:
        main()
