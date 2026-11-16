# 3D anatomy in the movement atlas: source, changes, licence

The bones, muscles, intervertebral discs and the body surface shown in the
movement atlas (`mesh-figure.glb`, `mesh-spine.glb`, `mesh-knee.glb`,
`mesh-shoulder.glb`) are derived from:

**BodyParts3D, © The Database Center for Life Science** — a 3D model of an adult
human male body (release 4.0, IS-A tree, polygon reduction 99 %).

- Download page: https://dbarchive.biosciencedbc.jp/en/bodyparts3d/download.html
- Archive used: https://dbarchive.biosciencedbc.jp/data/bodyparts3d/LATEST/isa_BP3D_4.0_obj_99.zip
  (fetched 2026-11-15; its checksum is recorded in the Oncofiles pull request for issue 841)
- Licence page: https://dbarchive.biosciencedbc.jp/en/bodyparts3d/lic.html

## Licence of the source

The licence page and the archive README (both updated 2025-02-27) state:
"The license for this database is specified in the Creative Commons Attribution
4.0 International", with the requested credit "BodyParts3D, © The Database
Center for Life Science licensed under CC Attribution 4.0 International"
(https://creativecommons.org/licenses/by/4.0/).

The OBJ files inside the release 4.0 archive (dated 2013) still carry the
earlier notice, "BodyParts3D, (c) The Database Center for Life Science licensed
under CC Attribution-Share Alike 2.1 Japan"
(https://creativecommons.org/licenses/by-sa/2.1/jp/).

## Licence of the derived meshes

To satisfy both notices, the four derived `mesh-*.glb` files are shared under
**Creative Commons Attribution-ShareAlike 4.0 International** (CC BY-SA 4.0).
The licence text: https://creativecommons.org/licenses/by-sa/4.0/legalcode If you reuse them, credit
BodyParts3D as above, say that the meshes were modified, and keep the same
licence. This applies to the mesh files only, not to the rest of Oncofiles.

## What was changed

Done by `scripts/atlas_meshes/build_meshes.py` in the Oncofiles repository
(reproducible from the archive above):

- selected parts only (list below), rotated to a y-up frame and moved to the
  joint centre, scaled to centimetres (areas) or metres (figure);
- duplicate vertices welded; large parts decimated, small parts smoothed by one
  Loop subdivision; long bones and muscles cut by a plane near the joint and
  the cut closed with a flat cap;
- thin articular-cartilage layers generated from the joint surfaces of the
  femur, tibia and patella; the humeral head split off so it can be drawn
  see-through;
- the skin (a thin double-walled shell in the source) rebuilt as one closed
  surface from a 3 mm voxel volume: small openings closed, the upper arms
  separated from the chest where the source skin is fused, the groin smoothed
  into a neutral mannequin shape (fingers and toes are merged by this step);
  then reduced to 30 000 triangles and given blend weights for 14 joints, so
  the figure can be posed for the exercises;
- landmarks measured from the meshes (joint centres, disc planes, facet
  joints, attachment points) stored in the files as numbers.

Not from BodyParts3D, drawn schematically by the page itself: knee menisci and
ligaments, the rim (labrum) of the shoulder socket, the deep back muscles
(multifidus), and every marker of the finding (fluid, cyst, worn-cartilage
tint, bone spurs, highlighted facet joints).

## Parts used (BodyParts3D element file, FMA concept, name)

| File | FMA | Name |
| --- | --- | --- |
| FJ2810 | FMA7163 | skin |
| FJ3365 | FMA24474 | right femur |
| FJ3387 | FMA24477 | right tibia |
| FJ3366 | FMA24480 | right fibula |
| FJ3381 | FMA24486 | right patella |
| FJ1433 | FMA38928 | right rectus femoris |
| FJ1443 | FMA38932 | right vastus medialis |
| FJ1442 | FMA38930 | right vastus lateralis |
| FJ1395 | FMA45888 | long head of right biceps femoris |
| FJ1435 | FMA22448 | right semimembranosus |
| FJ1430 | FMA22591 | right popliteus |
| FJ3162 | FMA13074 | third lumbar vertebra |
| FJ3165 | FMA13075 | fourth lumbar vertebra |
| FJ3168 | FMA13076 | fifth lumbar vertebra |
| FJ3393 | FMA16202 | sacrum |
| FJ3215 | FMA16035 | intervertebral disk of third lumbar vertebra |
| FJ3216 | FMA16036 | intervertebral disk of fourth lumbar vertebra |
| FJ3217 | FMA16037 | intervertebral disk of fifth lumbar vertebra |
| FJ3152 | FMA16586 | right hip bone |
| FJ3288 | FMA16587 | left hip bone |
| FJ1527, FJ1527M | FMA22740, FMA22741 | right and left iliocostalis lumborum |
| FJ1535, FJ1535M | FMA22751, FMA22753 | right and left longissimus thoracis |
| FJ1431, FJ1431M | FMA22342, FMA22343 | right and left psoas major |
| FJ3279 | FMA13396 | left scapula |
| FJ3237 | FMA13323 | left clavicle |
| FJ3262 | FMA23131 | left humerus |
| FJ1506M | FMA32545 | left supraspinatus |
| FJ1500M | FMA32548 | left infraspinatus muscle |
| FJ1504M | FMA13415 | left subscapularis |
| FJ1468M, FJ1467M, FJ1513M | FMA34681, FMA34683, FMA34685 | clavicular, acromial and spinal part of left deltoid |
| FJ1478M | FMA37687 | long head of left biceps brachii |

Read for measurements only (joint centres of the figure), not shipped: left
femur (FJ3259), left tibia (FJ3282), right humerus (FJ3368), right and left
radius (FJ3349, FJ3277), seventh cervical vertebra (FJ3172), twelfth thoracic
vertebra (FJ3156).

## Limits

This is general, illustrative anatomy of one reference body. It is not the
anatomy of the person whose findings the atlas shows, and it was not made from
his images. The findings are drawn onto it from the text of the reports.

## three.js

`three.module.js`, `GLTFLoader.js` and `BufferGeometryUtils.js` are three.js
r149 (MIT licence, Copyright 2010-2023 three.js authors), official files with
only the import paths made relative.
