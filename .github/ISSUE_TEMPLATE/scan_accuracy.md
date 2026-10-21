---
name: Scan accuracy report
about: Share how a scan compared with the real part (good or bad results help)
labels: accuracy
---

**The part**
- What it is, rough size:
- Material and colour (black, shiny, transparent...):
- Reference: caliper or CAD?

**The scan**
- One side or both sides:
- Torch used for close-ups: yes / no
- Mat scale set (`PHONE2CAD_MAT_10_SQUARES_MM`): yes / no
- Number of frames:

**Results**
| Measurement | Phone2CAD | ± | Real value |
|---|---|---|---|
| | | | |

If you compared with a CAD model, paste the output of
`python -m phone2cad_worker.compare mesh.ply part.stl`.

Screenshots of the 3D copy are welcome; please do not attach raw photos that
show your room.
