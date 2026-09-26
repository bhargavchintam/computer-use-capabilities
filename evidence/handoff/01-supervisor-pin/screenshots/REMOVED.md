# Screenshots removed

This run used a visible (headed) browser on a 2x (Retina) display. The values painted over in
screenshots were placed in CSS pixels on a 2x image, so some synthetic values (the operator id,
the member number, the nickname) were not covered. Found in final review and fixed: screenshots
are now taken at CSS scale, and `tests/integration/test_masking.py` emulates a 2x display.
The redacted text snapshots and the event log in this folder were not affected.
