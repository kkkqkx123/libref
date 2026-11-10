using System;
using IO = System.IO;

namespace Store {
// Cache holds fetched items.
class Cache : Base {
    /// Return the stored count.
    public int Get(string key) {
        return this.Lookup(key);
    }

    public void Run(string path) {
        var cache = new Cache();
        cache.Get(path);
        IO.File.Delete(path);
        System.Console.WriteLine(path);
    }
}
}
