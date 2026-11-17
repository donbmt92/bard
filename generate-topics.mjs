// Sinh nhiều topic SEO cho Peptira, mỗi vòng lặp = 1 request = 1 topic.
// Chạy:  ANTHROPIC_API_KEY=sk-ant-... node generate-topics.mjs
// Kết quả: topics.json (ghi lại sau mỗi request, chạy lại sẽ bỏ qua topic đã có)

import fs from "node:fs";

// ---------- CẤU HÌNH ----------
const CONFIG = {
  brand: "Peptira",
  site: "Peptira.com",
  code: "BF2026",
  discount: "Up to 50% Off",
  event: "Black Friday 2026",
  backupCode: "10OFF",
  link: "[LINK]", // thay bằng URL trang coupon của bạn
  model: "claude-haiku-5-5",
  outFile: "topics.json",
  delayMs: 1500,
};

// Mỗi keyword x mỗi angle = 1 topic (12 x 4 = 48 topic). Thêm/bớt tùy ý.
const KEYWORDS = [
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
];

const ANGLES = [
  "how-to: step-by-step guide to applying the code at checkout",
  "deal summary: short overview of the deal with a details list (code, discount, where it works, status)",
  "FAQ: 3-4 short questions and answers about the code",
  "troubleshooting: what to check if the code does not apply, including the backup code",
];

// ---------- PROMPT ----------
const SYSTEM = `You write forum topics for coupon/deal SEO. Each request you write exactly ONE topic.

Facts you may use (do not add any others):
- Store: {brand} ({site}), sells research peptides, research use only.
- Coupon code: {code}
- Discount: {discount}
- Event: {event}
- Code status: active, confirmed by {brand}.
- Backup code if the main one does not apply: {backupCode}
- Link for the full list of codes: {link}

Rules for the TITLE:
- Must contain the target keyword exactly, the code in square brackets [{code}], "{discount}" (or "Save Up to 50%"), and "Black Friday".
- 50-70 characters. No emojis, no ALL CAPS words except the code.
- Must be clearly different in wording and word order from every title in the "already used" list.

Rules for the MESSAGE:
- English, 120-180 words, plain text (no markdown, no HTML), short paragraphs or simple numbered/dashed lists.
- Use the target keyword in the first sentence and once more later. Mention the code {code} 2-3 times.
- Follow the requested angle.
- Include the link line once, near the end: "Full list of verified {brand} codes: {link}"
- End with: "Note: {brand} products are sold for research use only."
- Do NOT invent anything: no prices, expiry dates, product names, exclusions, stacking rules, shipping offers, testimonials or personal experience ("I saved...", "I tested..."). No health, medical or results claims.

Output: only a JSON object, no code fence, no extra text:
{"title": "...", "message": "..."}`;

const fill = (s) => s.replace(/\{(\w+)\}/g, (m, k) => CONFIG[k] ?? m);

const userPrompt = (keyword, angle, usedTitles) =>
  `Target keyword: ${keyword}
Angle: ${angle}
Already used titles (do not repeat or closely imitate):
${usedTitles.length ? usedTitles.map((t) => "- " + t).join("\n") : "(none)"}`;

// ---------- 1 REQUEST ----------
async function generateOne(keyword, angle, usedTitles) {
  const res = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-api-key": process.env.ANTHROPIC_API_KEY,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify({
      model: CONFIG.model,
      max_tokens: 1000,
      system: fill(SYSTEM),
      messages: [{ role: "user", content: userPrompt(keyword, angle, usedTitles) }],
    }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  const data = await res.json();
  const text = data.content.map((b) => b.text ?? "").join("");
  const json = text.slice(text.indexOf("{"), text.lastIndexOf("}") + 1);
  const topic = JSON.parse(json);
  if (!topic.title?.includes(`[${CONFIG.code}]`)) throw new Error("Title thiếu mã: " + topic.title);
  return topic;
}

// ---------- LOOP ----------
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  if (!process.env.ANTHROPIC_API_KEY) throw new Error("Thiếu ANTHROPIC_API_KEY");
  const topics = fs.existsSync(CONFIG.outFile)
    ? JSON.parse(fs.readFileSync(CONFIG.outFile, "utf8"))
    : [];
  const done = new Set(topics.map((t) => `${t.keyword}|${t.angle}`));

  for (const keyword of KEYWORDS) {
    for (const angle of ANGLES) {
      if (done.has(`${keyword}|${angle}`)) continue;
      const usedTitles = topics.map((t) => t.title);
      for (let attempt = 1; attempt <= 3; attempt++) {
        try {
          const topic = await generateOne(keyword, angle, usedTitles);
          topics.push({ keyword, angle, ...topic });
          fs.writeFileSync(CONFIG.outFile, JSON.stringify(topics, null, 2));
          console.log(`[${topics.length}] ${topic.title}`);
          break;
        } catch (err) {
          console.error(`Lỗi (${attempt}/3) "${keyword}": ${err.message}`);
          await sleep(3000 * attempt);
        }
      }
      await sleep(CONFIG.delayMs);
    }
  }
  console.log(`Xong: ${topics.length} topic -> ${CONFIG.outFile}`);
}

main();
