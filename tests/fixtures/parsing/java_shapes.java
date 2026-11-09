import java.util.List;
import java.util.*;

class Mapper<T> extends Base implements Runnable {
    @Override
    public void run() {
        obj.bar(1);
        this.baz();
        bar(2);
        new Foo<String>(1);
    }

    public T convert(T input) {
        return input;
    }

    public T convert(T input, String flag) {
        return input;
    }

    class Inner {
        void inner() {
            Mapper.this.baz();
        }
    }

    enum Kind {
        A, B
    }
}
