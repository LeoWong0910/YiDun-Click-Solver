// Analysis-only oracle: loads the captured core bundle and returns module 0x19
// (aes/xorEncode/xorDecode).  The final solver does not import or execute this.
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const [op, ...args] = process.argv.slice(2);
const moduleId = op === "keys" ? Number(args[0]) : 0x0a;
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const corePath = path.join(root, "reverse", "assets", "core-optimi.deob.strings.js");
let src = fs.readFileSync(corePath, "utf8");

src = src.replace(/_0x8fe10e\(0x0\)\r?\n\s*\);/, `_0x8fe10e(${moduleId})\n    );`);

const math = Object.create(Math);
let seq = [];
math.random = () => (seq.length ? seq.shift() : 0.123456789);

const ctx = {
  window: {
    encodeURIComponent,
    decodeURIComponent,
    Math: math,
    parseInt,
  },
  document: {
    getElementById() {
      return null;
    },
    createElement() {
      return {
        style: {},
        setAttribute() {},
        contentWindow: { parseInt },
      };
    },
    body: {
      appendChild() {},
      removeChild() {},
    },
  },
  Math: math,
  console,
};
ctx.global = ctx;
vm.createContext(ctx);
vm.runInContext(src, ctx, { filename: "core-optimi.deob.strings.js" });

const crypto = ctx.window.NECaptcha;

if (op === "xor") {
  console.log(crypto.xorEncode(args[0], args[1]));
} else if (op === "aes") {
  seq = [0.1, 0.2, 0.3, 0.4];
  console.log(crypto.aes(args[0] ?? ""));
} else if (op === "keys") {
  console.log(JSON.stringify(Object.keys(crypto)));
  console.log(String(crypto).slice(0, 200));
} else {
  console.error("usage: node tools/js_crypto_oracle.mjs xor <token> <text> | aes <text>");
  process.exit(2);
}
