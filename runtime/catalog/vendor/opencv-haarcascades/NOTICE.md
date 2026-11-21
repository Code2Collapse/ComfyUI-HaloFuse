# OpenCV Haar cascades (vendored)

Source: https://github.com/opencv/opencv, tag 4.12.0 = commit 49486f61fb25722cbcf586b7f4320921d46fb38e,
directory `data/haarcascades/`. Downloaded 2026-11-20; files are byte-identical to that commit (see `SHA256SUMS`).

Why vendored: OpenCV 5 wheels no longer ship the cascade XML files. ComfyUI-EasyColorCorrector and the WAS node
suite load them from `cv2.data.haarcascades`; the image build used to download them from a mutable tag on every
build without a checksum (audit finding X13). The Dockerfile now copies these files and verifies `SHA256SUMS`.

Licences:
- 15 files carry the "Intel License Agreement For Open Source Computer Vision Library" header (BSD-style); the header
  is kept inside each file, which satisfies its notice condition.
- `haarcascade_license_plate_rus_16stages.xml` and `haarcascade_russian_plate_number.xml` carry no header and are
  distributed under the OpenCV repository licence at that commit, Apache License 2.0 (`LICENSE-OpenCV-Apache-2.0.txt`).
