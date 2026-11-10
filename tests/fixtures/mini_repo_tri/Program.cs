using System;

namespace Demo {
class Greeter {
    public string Say(string name) {
        return this.Helper(name);
    }

    private string Helper(string name) {
        System.Console.WriteLine(name);
        return name;
    }
}
}
