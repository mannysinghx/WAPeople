-- Search should find Steven C. González from 'Gonzalez' and Rebecca Saldaña from 'Saldana'. A plain function,
-- not the unaccent extension: nothing to install, and the tables it reads are small enough to scan.
CREATE OR REPLACE FUNCTION fold_accents(value text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT translate(value, 'áàâäãåāéèêëēíìîïīóòôöõøōúùûüūñçšžýÁÀÂÄÃÅĀÉÈÊËĒÍÌÎÏĪÓÒÔÖÕØŌÚÙÛÜŪÑÇŠŽÝ',
                          'aaaaaaaeeeeeiiiiiooooooouuuuuncszyAAAAAAAEEEEEIIIIIOOOOOOOUUUUUNCSZY')
$$;
