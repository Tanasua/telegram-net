import assert from "node:assert/strict";
import { test } from "node:test";

import { tick } from "../src/index.js";

function fakeKV(initial = null) {
  const store = new Map(initial ? [["state", initial]] : []);
  return { writes: 0, get: async (k) => store.get(k) ?? null, put: async function (k, v) { this.writes++; store.set(k, v); }, store };
}

const page = (msgs) =>
  msgs
    .map(([id, text, time]) => `<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="zhytomyrmilitaryadministration/${id}"><div class="tgme_widget_message_text js-message_text" dir="auto">${text}</div><time datetime="${time}"></time></div></div>`)
    .join("");

test("tick: читає t.me/s, надсилає в Telegram, пише KV лише при змінах", async () => {
  const now = Date.parse("2026-11-14T20:05:00Z");
  const sent = [];
  let html = page([[10, "Новина", "2026-11-14T19:00:00Z"]]);
  globalThis.fetch = async (url, opts) => {
    if (String(url).startsWith("https://t.me/s/")) return new Response(html, { status: 200 });
    sent.push(JSON.parse(opts.body));
    return new Response(JSON.stringify({ ok: true, result: { message_id: 1 } }));
  };
  const env = { ALERTS_STATE: fakeKV(), TELEGRAM_BOT_TOKEN: "x" };

  let r = await tick(env, now);
  assert.equal(r.sent, 0);
  assert.equal(env.ALERTS_STATE.writes, 1); // перший запуск — зафіксували курсор

  r = await tick(env, now);
  assert.equal(env.ALERTS_STATE.writes, 1); // без змін — без запису

  html = page([[10, "Новина", "2026-11-14T19:00:00Z"], [11, "🟡 Коростенський район — повітряна тривога, жовтий рівень", "2026-11-14T20:04:30Z"]]);
  r = await tick(env, now);
  assert.equal(r.sent, 2); // Коростень і Малин
  assert.deepEqual(sent.map((s) => s.chat_id).sort(), ["@mykorostennews", "@mymalynnews"]);
  assert.equal(sent[0].parse_mode, "HTML");
  assert.equal(env.ALERTS_STATE.writes, 2);
});

test("tick: збій Telegram для одного каналу не зупиняє інші", async () => {
  const now = Date.parse("2026-11-14T20:05:00Z");
  const state = JSON.stringify({ cursor: 10, active: {} });
  const html = page([[11, "🔴 Житомирська область — повітряна тривога, червоний рівень", "2026-11-14T20:04:50Z"]]);
  let calls = 0;
  globalThis.fetch = async (url) => {
    if (String(url).startsWith("https://t.me/s/")) return new Response(html, { status: 200 });
    calls++;
    return new Response(JSON.stringify(calls === 1 ? { ok: false, description: "chat not found" } : { ok: true }));
  };
  const env = { ALERTS_STATE: fakeKV(state), TELEGRAM_BOT_TOKEN: "x" };
  const r = await tick(env, now);
  assert.equal(r.sent, 7); // уся область — усі 7 каналів, хоч один і відмовив
  assert.equal(calls, 7);
});

test("tick: недоступне джерело — помилка, стан не чіпаємо", async () => {
  globalThis.fetch = async () => new Response("", { status: 302 });
  const env = { ALERTS_STATE: fakeKV(JSON.stringify({ cursor: 5, active: {} })), TELEGRAM_BOT_TOKEN: "x" };
  await assert.rejects(() => tick(env), /302/);
  assert.equal(env.ALERTS_STATE.writes, 0);
});
