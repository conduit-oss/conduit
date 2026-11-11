# removed-api-sdk fixture

Made-up library used only by Conduit unit tests for issue #141.

`widgetkit` v1 ships:
- `widgetkit.error.GoneError`
- `Widget.create` defined at the internal path `widgetkit._internal.widget.Widget.create`
  and re-exported as `widgetkit.Widget`

v2 removes the error module and replaces `create` with `build`.

Nothing here is a real public SDK. Do not treat these trees as feed or
parity fixtures.
