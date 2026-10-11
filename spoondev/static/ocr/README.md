# Browser OCR assets

Pinned, self-hosted Tesseract.js worker 6.0.1, Tesseract.js Core 6.0.0 and tessdata_fast 4.1.0 Japanese/English language models. Images stay in the browser. No CDN or paid OCR API is used at runtime.

`fan-image.js` owns the worker and speaks the fixed version's `load`, `loadLanguage`, `initialize`, `setParameters` and `recognize` job protocol. This lets cancellation and timeouts terminate the worker even during initialization. Updating these assets requires rerunning browser OCR checks. The non-SIMD LSTM core embeds its WASM; a separate `.wasm` file is not needed.

`manifest.json` records upstream sources, npm package integrity and SHA-256 checksums. Language models are deterministically gzip-compressed locally (mtime=0). Serve `.gz` as compressed binary data, without HTTP Content-Encoding: the worker decompresses it. License files and bundled dependency notices are retained here.
