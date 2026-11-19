# SliceDeck

An independent SliceYT-inspired clipping workspace. This is not affiliated with SliceYT or YouTube.

## Features and limits
- Preview public embeddable YouTube videos and enter start/end times.
- YouTube MP4 export requires the optional separately run [local companion](backend/README.md). GitHub Pages hosts only static files; it does not process YouTube video. The companion runs on the same computer as the browser by default, not on your phone. The local frontend requests up to 1080p MP4 (actual resolution depends on the available source and is never upscaled); an updated compatible companion is required. Compatible browsers may require local-network permission.
- The browser-only local-file WebM recorder is experimental and is not verified to work reliably. It records in real time, may not preserve audio, and is not supported in every browser. Keep the tab active.
- No accounts, hosted storage, 4K, Google Drive, paid plans, or background processing.
- Use only material you have the rights to, with platform authorization as applicable. Some videos cannot be embedded or exported. The tool does not bypass login, private, age or geographic restrictions.

The source is published publicly. Do not put secrets, private videos or personal data in this repository.
