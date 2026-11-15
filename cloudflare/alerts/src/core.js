// Логіка сигналів тривоги й відбою — без залежностей від Cloudflare, щоб її
// можна було тестувати звичайним `node --test`. Повторює src/telegram_net/alerts.py.

export const DISTRICTS = ["Житомирський", "Бердичівський", "Коростенський", "Звягельський"];
export const MAX_EVENT_AGE_MS = 10 * 60 * 1000; // після простою застарілі сигнали не публікуємо

const ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", "#39": "'" };

function decode(html) {
  return html
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&(#x[0-9a-f]+|#\d+|\w+);/gi, (m, e) => {
      if (e[0] === "#") {
        const code = e[1].toLowerCase() === "x" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
        return Number.isFinite(code) ? String.fromCodePoint(code) : m;
      }
      return ENTITIES[e] ?? m;
    })
    .split("\n")
    .map((line) => line.replace(/[ \t ]+/g, " ").trim())
    .join("\n")
    .trim();
}

/** Повідомлення сторінки t.me/s/<канал>: [{id, text, time}] від старішого до новішого. */
export function parsePage(html) {
  const out = [];
  for (const chunk of html.split('<div class="tgme_widget_message_wrap').slice(1)) {
    const post = chunk.match(/data-post="[^"/]+\/(\d+)"/);
    if (!post || chunk.includes("service_message")) continue;
    const textMatch = chunk.match(/class="tgme_widget_message_text js-message_text"[^>]*>([\s\S]*?)<\/div>/);
    const timeMatch = chunk.match(/<time datetime="([^"]+)"/);
    const text = textMatch ? decode(textMatch[1]) : "";
    if (!text) continue;
    out.push({ id: Number(post[1]), text, time: timeMatch ? timeMatch[1] : null });
  }
  return out.sort((a, b) => a.id - b.id);
}

/** «🟡 Коростенський район — повітряна тривога, жовтий рівень: Дронова загроза» → подія або null. */
export function parseAlert(text) {
  const line = text.trim().split("\n")[0].trim();
  if (!line.includes("—")) return null;
  const idx = line.indexOf("—");
  const region = line.slice(0, idx).toLowerCase();
  const rest = line.slice(idx + 1);
  const restLow = rest.toLowerCase();
  let kind;
  if (restLow.includes("відбій") && restLow.includes("тривог")) kind = "clear";
  else if (restLow.includes("повітряна тривога")) kind = "alert";
  else return null;

  let districts;
  if (region.includes("област")) {
    if (!region.includes("житомирськ")) return null;
    districts = [...DISTRICTS];
  } else if (region.includes("район")) {
    districts = DISTRICTS.filter((d) => region.includes(d.slice(0, -2).toLowerCase()));
  } else {
    return null;
  }
  if (!districts.length) return null;

  let level = null;
  let threat = null;
  if (kind === "alert") {
    level = restLow.includes("червон") ? "red" : restLow.includes("жовт") ? "yellow" : null;
    if (rest.includes(":")) {
      threat = rest
        .split(":")
        .slice(1)
        .join(":")
        .replace(/\s*\((?:жовтий|червоний) рівень\)\s*/gi, "")
        .trim()
        .replace(/^[\s.]+|[\s.]+$/g, "");
      threat = threat ? threat[0].toUpperCase() + threat.slice(1) : null;
    }
  }
  return { kind, districts, level, threat };
}

export function formatDuration(ms) {
  let minutes = Math.max(1, Math.round(ms / 60000));
  const hours = Math.floor(minutes / 60);
  minutes %= 60;
  if (hours && minutes) return `${hours} год ${minutes} хв`;
  return hours ? `${hours} год` : `${minutes} хв`;
}

export function renderAlert(event, district, startedAt, at) {
  if (event.kind === "clear") {
    let text = `🟢 <b>Відбій повітряної тривоги</b>\n${district} район`;
    if (startedAt && at > startedAt) text += `\nТривога тривала ${formatDuration(at - startedAt)}.`;
    return text;
  }
  const icon = event.level === "red" ? "🔴" : "🟡";
  const level = { red: ", червоний рівень", yellow: ", жовтий рівень" }[event.level] ?? "";
  let text = `${icon} <b>Повітряна тривога${level}</b>\n${district} район`;
  if (event.threat) text += `\n${event.threat}.`;
  return text;
}

/**
 * Один крок: нові повідомлення джерела → що надіслати й новий стан.
 * state = {cursor: id останнього обробленого | null, active: {район: ISO початку тривоги}}.
 * Перший запуск (cursor = null) лише відновлює стан з історії й нічого не надсилає.
 */
export function step(state, items, nowMs, channelsByDistrict) {
  const next = { cursor: state.cursor ?? null, active: { ...(state.active ?? {}) } };
  const bootstrap = next.cursor === null;
  const sends = [];
  for (const item of items) {
    if (next.cursor !== null && item.id <= next.cursor) continue;
    const event = parseAlert(item.text);
    const atMs = item.time ? Date.parse(item.time) : nowMs;
    if (event) {
      const fresh = !bootstrap && nowMs - atMs <= MAX_EVENT_AGE_MS;
      for (const district of event.districts) {
        const startedMs = next.active[district] ? Date.parse(next.active[district]) : null;
        if (event.kind === "alert") {
          if (!next.active[district]) next.active[district] = new Date(atMs).toISOString();
        } else {
          delete next.active[district];
        }
        if (fresh) {
          const text = renderAlert(event, district, startedMs, atMs);
          for (const chat of channelsByDistrict[district] ?? []) sends.push({ chat, text });
        }
      }
    }
    next.cursor = next.cursor === null ? item.id : Math.max(next.cursor, item.id);
  }
  return { state: next, sends };
}
