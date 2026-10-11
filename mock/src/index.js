// Freya Bank statement services mock (Cloudflare Worker).
// Data-driven: every answer comes from src/customers.json (the bank's test customers, copied as given).
// A request names a service (by path), a customer number, and for card services a card number,
// plus the requested week for statement.week.decide / statement.week.change.
import DATA from "./customers.json";

const CUSTOMERS = new Map(DATA.customers.map((c) => [String(c.customerNo), c]));

// path -> service name. Filled from the integration document; the generic rule below covers "/cards/list",
// "/cards.list", "/v1/cards/list" and the like.
const PATH_ALIASES = {};

function serviceFromPath(pathname) {
  let p = decodeURIComponent(pathname).toLowerCase().replace(/\/+$/, "");
  if (PATH_ALIASES[p]) return PATH_ALIASES[p];
  const segs = p.split("/").filter(Boolean);
  const last = segs[segs.length - 1] || "";
  if (last.includes(".")) return last;                       // /voice/v1/cards.list -> cards.list
  const rest = segs.filter((x) => !/^(api|v\d+|voice|mock|services?)$/.test(x));
  return rest.join(".");                                     // /cards/list -> cards.list
}

const isTemplate = (v) => typeof v === "string" && (v.startsWith("{{") || v.startsWith("%7B%7B"));

const pick = (o, names) => {
  for (const n of names) if (o && o[n] !== undefined && o[n] !== null && o[n] !== "") return o[n];
  return undefined;
};
const digits = (s) => String(s ?? "").replace(/\D/g, "");

function findCardKey(entry, cardNumber) {
  const keys = Object.keys(entry);
  if (cardNumber === undefined) return keys.length === 1 ? keys[0] : undefined;
  const c = String(cardNumber);
  if (entry[c]) return c;
  const last4 = digits(c).slice(-4), first = digits(c).slice(0, 4);
  const hits = keys.filter((k) => digits(k).slice(-4) === last4 && (!first || digits(k).startsWith(first.slice(0, 4))));
  if (hits.length === 1) return hits[0];
  const hits4 = keys.filter((k) => digits(k).slice(-4) === last4);
  return hits4.length === 1 ? hits4[0] : undefined;
}

const json = (status, body) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json; charset=utf-8" } });

let GUIDN = 0;
const guid = () => "f0e1d2c3b4a59687786950000" + String(1000000 + (GUIDN++ % 900000)).slice(-7);
function err(status, code, type, message, rootCause) {
  const g = guid();
  return json(status, { result: { guid: g, returnCode: code, messageText: message, code: code, message: message, type: type,
    details: { guid: g, rootCause: rootCause, errorOrigin: "ivr-gateway-test:ivr-channel-v0" } } });
}
// contract section 4: the 404 of each service, else 400 INVALID_ARGUMENTS_ERROR
const NOT_FOUND = {
  "cards.list": [404, "FB_IVR_CHANNEL_102", "Müşteriye ait geçerli kart bilgisi bulunamadı."],
  "cards.balance": [404, "FB_IVR_CHANNEL_120", "Kart bakiye servisi hatası: kart bulunamadı"],
  "statement.week.get": [404, "FB_IVR_CHANNEL_121", "Ekstre hafta servisi hatası: kart bulunamadı"],
  "statement.pdf.send": [404, "FB_IVR_CHANNEL_107", "Pdf dokümanı bulunamadı"],
};
function notFound(service) {
  const nf = NOT_FOUND[service];
  if (nf) return err(nf[0], nf[1], "WARNING", nf[2], "com.freyabank.ivrgateway.ivrchannel.rest.exception.NotFoundException");
  return err(400, "INVALID_ARGUMENTS_ERROR", "ERROR", "invalid arguments", "ValidationException");
}
const invalid = (msg) => err(400, "INVALID_ARGUMENTS_ERROR", "ERROR", msg, "ValidationException");
const SERVICES = new Set(["cards.list", "cards.balance", "statement.week.get", "statement.week.decide", "statement.week.change", "contact.email.get", "statement.pdf.send"]);
const CARD_SERVICES = new Set(["cards.balance", "statement.week.get", "statement.week.decide", "statement.week.change", "statement.pdf.send"]);

export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Allow-Headers": "*" } });
    }
    if (url.pathname === "/" || url.pathname === "/health") {
      return json(200, { ok: true, customers: CUSTOMERS.size });
    }
    let body = {};
    if (request.method !== "GET") {
      try { body = await request.json(); } catch (e) { body = {}; }
    }
    const q = Object.fromEntries(url.searchParams.entries());
    const args = { ...q, ...(body && typeof body === "object" ? body : {}) };
    const nested = args.data && typeof args.data === "object" ? args.data : {};
    const all = { ...nested, ...args };

    if (url.pathname.toLowerCase().endsWith("/oauth2/token")) {
      return json(200, { access_token: "test-token", token_type: "Bearer", expires_in: 1800 });
    }
    const service = serviceFromPath(url.pathname);
    if (!SERVICES.has(service)) return json(404, { hata: "bulunamadı", service: service });
    const customerNo = pick(all, ["customerNo", "customerNumber", "customer_no"]);
    const cardNumber = pick(all, ["cardNumber"]);
    const week = pick(all, ["newStatementWeek"]);
    const custNo = String(customerNo ?? "").trim();

    if (CARD_SERVICES.has(service) && service !== "statement.pdf.send" && (cardNumber === undefined || String(cardNumber).trim() === ""))
      return invalid("card number cannot be empty");
    if (["statement.week.decide", "statement.week.change"].includes(service) && (week === undefined || String(week).trim() === ""))
      return invalid("new statement week cannot be empty");
    if (["cards.list", "contact.email.get", "statement.pdf.send"].includes(service) && !custNo)
      return invalid("customer number cannot be empty");

    // card services are looked up by card number across all customers (the card number is unique in the data)
    let cust = custNo ? CUSTOMERS.get(custNo) : undefined;
    if (!cust && isTemplate(customerNo)) cust = DATA.customers[0];
    if (!cust && cardNumber !== undefined && service !== "statement.pdf.send") {
      cust = DATA.customers.find((c) => c.services[service] && c.services[service].http === undefined && c.services[service][String(cardNumber)]);
    }
    if (!cust) return notFound(service);
    let entry = cust.services[service];
    if (!entry) return notFound(service);

    if (entry.http === undefined) {
      const c = isTemplate(cardNumber) ? Object.keys(entry)[0] : String(cardNumber ?? "");
      if (!entry[c]) return notFound(service);       // exact masked number as cards.list gave it
      entry = entry[c];
    }
    if (entry.http === undefined) {
      const w = String(week ?? "").replace(/\D/g, "");
      if (!w || !entry[w]) return notFound(service);
      entry = entry[w];
    }
    const delay = Number(entry.delaySeconds || 0);
    if (delay > 0) await new Promise((r) => setTimeout(r, Math.min(delay, 60) * 1000));
    return json(entry.http, entry.response);
  },
};
