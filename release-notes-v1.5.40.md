# PotatoFlow v1.5.40

This maintenance release makes record-only post-processing resumable.

- Produces a visible MP4 as soon as ASS subtitle burn-in completes.
- Keeps the burned MP4 when AI cover generation or a later packaging stage fails.
- Preserves completed stage metadata when retrying a failed record-only task.
- Reuses valid ASS, burned-video, and cover artifacts instead of repeating expensive completed work.
- Excludes the intermediate burned MP4 from orphan-upload recovery while the remaining local stages continue.

When cover generation fails after a successful burn, retrying now continues from cover generation instead of encoding the video again.
