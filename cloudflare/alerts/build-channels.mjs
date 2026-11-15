// Генерує src/channels.json з data/sources_seed.json: які канали отримують
// сигнали якого району. Запускається перед кожним розгортанням.
import { readFileSync, writeFileSync } from "node:fs";

const seed = JSON.parse(readFileSync(new URL("../../data/sources_seed.json", import.meta.url), "utf8"));
const channels = seed.channels
  .filter((c) => c.active && c.telegram_chat_id && c.alert_district)
  .map((c) => ({ channel_id: c.channel_id, telegram_chat_id: c.telegram_chat_id, alert_district: c.alert_district }));
const out = { source: seed.settings?.alerts_source ?? "zhytomyrmilitaryadministration", channels };
writeFileSync(new URL("./src/channels.json", import.meta.url), JSON.stringify(out, null, 2) + "\n");
console.log(`channels.json: ${channels.length} каналів, джерело @${out.source}`);
