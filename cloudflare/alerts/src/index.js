// Cloudflare Worker: сигнали тривоги й відбою з каналу Житомирської ОВА в канали
// мережі «Мій …» за районами. Запускається щохвилини (cron) і перевіряє джерело
// двічі за запуск — затримка до ~30 секунд. Стан — у KV (ALERTS_STATE), пишеться
// лише коли щось змінилось, щоб не виходити за безкоштовний ліміт записів.
import { parsePage, renderSignature, step } from "./core.js";
import config from "./channels.json" with { type: "json" };

const STATE_KEY = "state";
const SECOND_TICK_DELAY_MS = 30_000;

function channelsByDistrict() {
  const map = {};
  for (const ch of config.channels) {
    (map[ch.alert_district] ??= []).push(ch.telegram_chat_id);
  }
  return map;
}

async function fetchItems(source) {
  const resp = await fetch(`https://t.me/s/${source}`, { redirect: "manual", headers: { "user-agent": "telegram-net-alerts/1.0" } });
  if (resp.status !== 200) throw new Error(`t.me/s/${source} → ${resp.status}`);
  return parsePage(await resp.text());
}

async function sendMessage(token, chat, text) {
  const resp = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: chat, text, parse_mode: "HTML", disable_web_page_preview: true }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!data.ok) throw new Error(`sendMessage ${chat}: ${data.description ?? resp.status}`);
}

export async function tick(env, now = Date.now()) {
  const raw = await env.ALERTS_STATE.get(STATE_KEY);
  const state = raw ? JSON.parse(raw) : { cursor: null, active: {} };
  const items = await fetchItems(config.source);
  const result = step(state, items, now, channelsByDistrict());
  for (const { chat, text } of result.sends) {
    const ch = config.channels.find((c) => c.telegram_chat_id === chat);
    try {
      await sendMessage(env.TELEGRAM_BOT_TOKEN, chat, ch ? `${text}\n\n${renderSignature(ch)}` : text);
    } catch (err) {
      console.error(String(err)); // один недоступний канал не має зупиняти інші
    }
  }
  const changed = JSON.stringify(result.state) !== JSON.stringify(state);
  if (changed) await env.ALERTS_STATE.put(STATE_KEY, JSON.stringify(result.state));
  return { sent: result.sends.length, changed, state: result.state };
}

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(
      (async () => {
        try {
          await tick(env);
          await new Promise((r) => setTimeout(r, SECOND_TICK_DELAY_MS));
          await tick(env);
        } catch (err) {
          console.error(String(err));
        }
      })(),
    );
  },

  // Перевірка стану: GET /status — курсор і райони, де зараз тривога. Без секретів.
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== "/status") return new Response("not found", { status: 404 });
    const raw = await env.ALERTS_STATE.get(STATE_KEY);
    return new Response(raw ?? "{}", { headers: { "content-type": "application/json; charset=utf-8" } });
  },
};
