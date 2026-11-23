# Quickstart

Install the client before running the examples.

## Examples

The first example connects and then closes the session.

```python
client = demo.Client()
client.connect("a")
client.close("a")
```

The second example repeats the same calls in the opposite order.

```python
client = demo.Client()
client.close("b")
client.connect("b")
```

`demo.Client.connect` accepts a timeout argument.
