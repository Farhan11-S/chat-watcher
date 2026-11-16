"""The 3D stage: a clipped moment re-enacted by a talent's official MMD model.

- models.py   the model library in <data_dir>/mmd (unpacked zips + a profile each)
- script.py   what a scene can say (shots, poses, faces, each set's cues) and checking one
- track.py    the voice track: the cut's audio, mouth shapes letter by letter, laughs
- store.py    table `stages`: which model, set and cut a moment uses, and its scene
- run.py      Claude writes the scene, and you direct it in words (prompt.py: what it is told)
- export.py   stage.mp4: the dashboard's 3D page stepped frame by frame in headless Chromium

The page that plays it is web/templates/stage.html with web/static/stage/ (three.js r171,
vendored in web/static/vendor/three-0.171: the last release with the MMD loader).

Licence (mmd.hololive.tv/terms): model files stay in <data_dir> and are only served to the
dashboard; every export carries "© COVER"."""
