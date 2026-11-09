import def, {a as b, c} from "mod/x";
import * as ns from "lib";
const d = require("r");
const {e, f: g} = require("r2");

export class Command extends Base {
  option(x, y = 1, ...rest) {
    this.register(x);
    return this;
  }
  async run(p) {
    this.option(p);
    obj.h().i(1);
    await helper(p);
    const dyn = await import("dyn");
    return dyn;
  }
}

const arrow = async (p) => helper(p);
const o = { k(v) { return v; } };
ns.done(b, c, d, e, g, arrow, o);
