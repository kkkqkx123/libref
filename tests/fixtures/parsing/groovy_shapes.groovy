package demo.store

import java.util.List
import static demo.Util.load

// line comment
/* block comment */
class Cache extends Base {
    String name = "c"

    Cache(String n) {
        this.name = n
    }

    def run() {
        def items = ["a"]
        println(items.first())
        this.helper(1, "x")
        load("k")
    }
}

def top(x) {
    x.toString()
}
