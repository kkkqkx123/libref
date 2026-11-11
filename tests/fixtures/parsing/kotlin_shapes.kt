package demo.store

import java.util.List
import demo.Util as U

// line comment
/* block comment */
class Cache<T>(val max: Int) : Repo {
    companion object {
        fun create(): Cache<Int> = Cache(10)
    }

    override fun find(id: Int): String? {
        val items: List<String> = listOf("a")
        return items.firstOrNull() ?: U.load(id.toString())
    }
}

fun top(x: Int, y: String = "d"): String = x.toString() + y
