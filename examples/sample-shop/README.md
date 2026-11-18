# shop (sample target repository)

A small checkout library used as a harder target for the MCP coding agent than the
`authkit` sample: five modules that depend on each other, prices in exact cents,
coupons, a "buy 2, get 1 free" promotion and sales tax.

Its issues (`examples/issues/sample-shop.json`) are harder in different ways: a bug
report that blames the wrong module, edge cases the existing tests don't cover, and
a promotion rule that has to be read carefully. All of the existing tests pass.

```bash
pytest -q
```
