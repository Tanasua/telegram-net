import assert from "node:assert/strict";
import { test } from "node:test";

import { formatDuration, parseAlert, parsePage, renderAlert, step } from "../src/core.js";

const msg = (id, text, time) => `
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="zhytomyrmilitaryadministration/${id}">
<div class="tgme_widget_message_text js-message_text" dir="auto">${text}</div>
<a class="tgme_widget_message_date" href="#"><time datetime="${time}" class="time">20:04</time></a></div></div>`;

test("парсинг сторінки t.me/s: текст, <br>, сутності, порядок", () => {
  const html =
    msg(58013, "🟢 Житомирський район — відбій повітряної тривоги", "2026-11-14T20:04:38+00:00") +
    msg(58012, "🟢 Коростенський район — відбій повітряної тривоги<br/><br/>⚠️ Зверніть увагу, повітряна тривога досі триває у:<br/>- Житомирський район", "2026-11-14T20:04:34+00:00") +
    msg(57000, "Нарада &amp; «підготовка»&nbsp;до зими", "2026-11-14T10:00:00+00:00");
  const items = parsePage(html);
  assert.deepEqual(items.map((i) => i.id), [57000, 58012, 58013]);
  assert.equal(items[0].text, "Нарада & «підготовка» до зими");
  assert.equal(items[1].text.split("\n")[0], "🟢 Коростенський район — відбій повітряної тривоги");
  assert.equal(items[2].time, "2026-11-14T20:04:38+00:00");
});

test("розбір реальних форматів сигналів ОВА", () => {
  assert.deepEqual(parseAlert("🟡 Коростенський район — повітряна тривога, жовтий рівень: Дронова загроза (жовтий рівень)"), {
    kind: "alert", districts: ["Коростенський"], level: "yellow", threat: "Дронова загроза",
  });
  assert.deepEqual(parseAlert("🔴 Житомирський, Коростенський, Звягельський райони — повітряна тривога, червоний рівень: ракетна загроза."), {
    kind: "alert", districts: ["Житомирський", "Коростенський", "Звягельський"], level: "red", threat: "Ракетна загроза",
  });
  assert.deepEqual(parseAlert("🟢 Житомирський район — відбій повітряної тривоги\n\n⚠️ Зверніть увагу, повітряна тривога досі триває у:\n- Бердичівський район"), {
    kind: "clear", districts: ["Житомирський"], level: null, threat: null,
  });
  assert.equal(parseAlert("🔴 Житомирська область — повітряна тривога").districts.length, 4);
  assert.equal(parseAlert("Київська область — повітряна тривога"), null);
  assert.equal(parseAlert("За добу п'ять разів оголошували повітряну тривогу"), null);
});

test("тексти сигналів і тривалість", () => {
  const start = Date.parse("2026-11-14T19:35:00Z");
  const at = Date.parse("2026-11-14T20:04:00Z");
  assert.equal(renderAlert({ kind: "alert", level: "red", threat: "Ракетна загроза" }, "Коростенський", null, at),
    "🔴 <b>Повітряна тривога, червоний рівень</b>\nКоростенський район\nРакетна загроза.");
  assert.equal(renderAlert({ kind: "clear" }, "Коростенський", start, at),
    "🟢 <b>Відбій повітряної тривоги</b>\nКоростенський район\nТривога тривала 29 хв.");
  assert.equal(formatDuration(135 * 60000), "2 год 15 хв");
  assert.equal(formatDuration(60 * 60000), "1 год");
});

const CHANNELS = { "Коростенський": ["@mykorostennews", "@mymalynnews"], "Житомирський": ["@myzhytomyrnews"] };
const NOW = Date.parse("2026-11-14T20:05:00Z");
const item = (id, text, minutesAgo) => ({ id, text, time: new Date(NOW - minutesAgo * 60000).toISOString() });

test("перший запуск лише відновлює стан, далі — сигнали у канали свого району", () => {
  const history = [item(1, "🟡 Коростенський район — повітряна тривога, жовтий рівень", 30), item(2, "Звичайна новина ОВА", 20)];
  let { state, sends } = step({ cursor: null, active: {} }, history, NOW, CHANNELS);
  assert.deepEqual(sends, []);
  assert.equal(state.cursor, 2);
  assert.ok(state.active["Коростенський"]);

  ({ state, sends } = step(state, history, NOW, CHANNELS));
  assert.deepEqual(sends, []); // нічого нового

  ({ state, sends } = step(state, [...history,
    item(3, "🟢 Коростенський район — відбій повітряної тривоги", 1),
    item(4, "🔴 Житомирський район — повітряна тривога, червоний рівень: Ракетна загроза (червоний рівень)", 0),
    item(5, "🟡 Бердичівський район — повітряна тривога, жовтий рівень", 0), // каналів цього району немає
  ], NOW, CHANNELS));
  assert.deepEqual(sends.map((s) => s.chat), ["@mykorostennews", "@mymalynnews", "@myzhytomyrnews"]);
  assert.match(sends[0].text, /Тривога тривала 29 хв\./);
  assert.match(sends[2].text, /червоний рівень/);
  assert.equal(state.cursor, 5);
  assert.deepEqual(Object.keys(state.active).sort(), ["Бердичівський", "Житомирський"]);
});

test("після простою застарілі сигнали не публікуються", () => {
  let { state } = step({ cursor: null, active: {} }, [item(1, "новина", 60)], NOW, CHANNELS);
  const r = step(state, [item(2, "🟡 Коростенський район — повітряна тривога, жовтий рівень", 25)], NOW, CHANNELS);
  assert.deepEqual(r.sends, []);
  assert.ok(r.state.active["Коростенський"]);
});
