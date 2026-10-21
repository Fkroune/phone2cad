// Phone2CAD v0 ground-truth artefact.
// A 40 x 30 x 20 mm block with a 10 mm through-hole along its height.
// The worker's acceptance test reads the parameters below, so keep the
// "name = value;" form when editing them.
//
// Print it in a matte, light colour (or dust it with matte spray) so the
// cameras can see texture on it.

block_x = 40;   // mm
block_y = 30;   // mm
block_z = 20;   // mm
hole_d  = 10;   // mm, through the full height
hole_x  = 20;   // hole centre from the block's corner, mm
hole_y  = 15;

$fn = 128;

difference() {
    cube([block_x, block_y, block_z]);
    translate([hole_x, hole_y, -1])
        cylinder(d = hole_d, h = block_z + 2);
}
