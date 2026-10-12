"""Application preparation, approval workflow, safe submission and tracking.

* ``field_mapping`` -- classify form fields and decide what may be auto-filled
* ``documents``     -- résumé / cover-letter files (path containment, sha256)
* ``repository``    -- SQL for application answers, lookups, timelines
* ``payload``       -- the deterministic approval payload and approval view
* ``service``       -- prepare / answer / decide / submit / mark-submitted ...
* ``commands``      -- CLI (``apply``, ``approvals``, ``applications``)
"""
