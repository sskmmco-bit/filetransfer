# Self-hosted Poppins (offline / airgapped)

The site loads Poppins from the local `.ttf` files in this folder — no Google
Fonts CDN, no internet needed at runtime. Poppins is licensed under the SIL Open
Font License, so self-hosting is allowed.

The `@font-face` rules (in `templates/base.html`) use these five weights:

| File                      | Weight | CSS weight |
|---------------------------|--------|------------|
| `Poppins-Regular.ttf`     | 400    | normal     |
| `Poppins-Medium.ttf`      | 500    | 500        |
| `Poppins-SemiBold.ttf`    | 600    | 600        |
| `Poppins-Bold.ttf`        | 700    | bold       |
| `Poppins-ExtraBold.ttf`   | 800    | 800        |

The other Poppins files in this folder (Light, Thin, Black, all italics) are not
referenced by the CSS — harmless to keep, safe to delete if you want a leaner build.

If a file is missing, the site falls back to Inter / system fonts — nothing breaks.

## Production note

This project uses WhiteNoise's ManifestStaticFilesStorage. After changing fonts,
run:
```
python manage.py collectstatic
```
The files then ship inside the app image — no internet required on the host.
