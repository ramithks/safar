-- A template is a look, not a property type.
--
-- The eight names the CHECK constraint allowed were as much categories of
-- hotel as they were designs — an owner choosing between "Homestay" and
-- "Hostel" was being asked what their hotel *is*, which the properties table
-- already knows, rather than what they want their page to look like.
--
-- `minimal` and `heritage` are the two the visual picker needed: a
-- conversion-first layout with almost no ornament, and a warm editorial one for
-- a property whose selling point is the building. Neither describes a category
-- of hotel, which is the point.
--
-- Nothing existing changes. The eight original values stay valid, every stored
-- row still satisfies the constraint, and a deployment running the old backend
-- against this schema is unaffected — it simply never writes the two new names.
-- `app/services/pms/website.TEMPLATES` carries the same list.

ALTER TABLE hotel_websites DROP CONSTRAINT IF EXISTS hotel_websites_template_check;

ALTER TABLE hotel_websites ADD CONSTRAINT hotel_websites_template_check
  CHECK (template IN (
    'boutique', 'resort', 'luxury', 'homestay', 'hostel',
    'business', 'beach', 'mountain', 'minimal', 'heritage'
  ));
