#!/usr/bin/env python3
"""L'icône de Quai — même famille que Sillon, Truchement, Drogman.

Ce que l'outil saisit, pas l'ustensile : ni console, ni manette (Antenne a déjà
un écran). Quai dépose un jeu dans la bibliothèque de l'Ally : on montre le
dépôt lui-même. Une ligne de quai au graphite, deux tuiles au lavis déjà
posées, et la troisième, au graphite, qui descend dans la place libre.

Charte commune : papier, graphite, lavis, et le bleu réservé à l'ombre portée.

    python3 dessin.py       # écrit AppIcon.icns dans ce dossier
"""
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path.home() / "sillon" / "icone"))
from dessin import T, GRAPHITE, LAVIS, fond, ombre       # la fabrique commune
from PIL import ImageDraw

ICI = pathlib.Path(__file__).resolve().parent


def quai():
    img = fond()
    l, h = int(T * 0.19), int(T * 0.27)            # une tuile, format jaquette
    sol = int(T * 0.72)                            # la ligne du quai
    ecart = int(T * 0.045)
    x0 = (T - (3 * l + 2 * ecart)) // 2
    r = int(T * 0.03)
    posees = [(x0, sol - h), (x0 + 2 * (l + ecart), sol - h)]
    descente = (x0 + l + ecart, sol - h - int(T * 0.2))

    def formes(d, c):
        for x, y in posees:
            d.rounded_rectangle([x, y, x + l, y + h], r, fill=c)
        x, y = descente
        d.rounded_rectangle([x, y, x + l, y + h], r, fill=c)
        d.rounded_rectangle([x0 - int(T * 0.05), sol + int(T * 0.03),
                             x0 + 3 * l + 2 * ecart + int(T * 0.05), sol + int(T * 0.06)], int(T * 0.015), fill=c)
    ombre(img, formes, decalage=(int(T * 0.012), int(T * 0.02)), flou=36, alpha=60)

    d = ImageDraw.Draw(img)
    for x, y in posees:
        d.rounded_rectangle([x, y, x + l, y + h], r, fill=LAVIS)
    # la place libre, en pointillé discret
    x, y = descente[0], sol - h
    pas = int(T * 0.03)
    for i in range(0, l, pas * 2):
        d.line([(x + i, sol), (x + min(i + pas, l), sol)], fill=LAVIS, width=int(T * 0.008))
    # la tuile qui descend, et deux traits de mouvement au-dessus
    x, y = descente
    d.rounded_rectangle([x, y, x + l, y + h], r, fill=GRAPHITE)
    for k, dx in enumerate((0.3, 0.7)):
        cx = x + int(l * dx)
        d.rounded_rectangle([cx - int(T * 0.008), y - int(T * 0.13) + k * int(T * 0.03),
                             cx + int(T * 0.008), y - int(T * 0.04)], int(T * 0.008), fill=LAVIS)
    # le quai
    d.rounded_rectangle([x0 - int(T * 0.05), sol + int(T * 0.03),
                         x0 + 3 * l + 2 * ecart + int(T * 0.05), sol + int(T * 0.06)], int(T * 0.015), fill=GRAPHITE)
    return img


def fabriquer():
    img = quai().resize((1024, 1024), 1)
    png = ICI / "AppIcon-1024.png"
    img.save(png)
    jeu = ICI / "AppIcon.iconset"
    jeu.mkdir(exist_ok=True)
    for taille in (16, 32, 64, 128, 256, 512, 1024):
        for suffixe, px in ((f"icon_{taille}x{taille}.png", taille),
                            (f"icon_{taille//2}x{taille//2}@2x.png", taille)):
            if suffixe.startswith("icon_0"):
                continue
            subprocess.run(["sips", "-z", str(px), str(px), str(png), "--out", str(jeu / suffixe)],
                           capture_output=True, check=False)
    subprocess.run(["iconutil", "-c", "icns", str(jeu), "-o", str(ICI / "AppIcon.icns")], check=True)
    print(f"  écrit : {ICI / 'AppIcon.icns'}")


if __name__ == "__main__":
    fabriquer()
