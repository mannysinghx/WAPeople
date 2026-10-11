// District map: MapLibre GL (open source) + OpenFreeMap base tiles + our district GeoJSON.
(function () {
  "use strict";
  const COLORS = { D: "#2f6fd6", R: "#d1423a", split: "#8e55c6", vacant: "#8a939b" };
  const WA_BOUNDS = [[-124.85, 45.54], [-116.92, 49.01]];
  const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const panelTitle = document.getElementById("panel-title");
  const panelBody = document.getElementById("panel-body");
  let features = [];
  let selected = null;
  let map = null;

  function el(tag, attrs, children) {
    const n = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => (k === "text" ? (n.textContent = v) : n.setAttribute(k, v)));
    (children || []).forEach((c) => n.appendChild(c));
    return n;
  }
  const PARTY = { D: "Democrat", R: "Republican" };

  function memberRow(m, role) {
    return el("li", {}, [
      el("span", { class: "muted small", text: role + " " }),
      el("a", { href: "/legislators/" + m.id, text: m.name }),
      el("span", { class: "party party-" + (m.party || "x"), text: " " + (PARTY[m.party] || m.party || "") }),
    ]);
  }

  function showPanel(f) {
    const p = f.properties;
    const sens = typeof p.senators === "string" ? JSON.parse(p.senators) : p.senators;
    const reps = typeof p.representatives === "string" ? JSON.parse(p.representatives) : p.representatives;
    panelTitle.textContent = "District " + p.district;
    panelBody.replaceChildren();
    const list = el("ul", { class: "list member-list" });
    (sens.length ? sens : [{ id: "", name: "Vacant", party: "" }]).forEach((m) => list.appendChild(memberRow(m, "Senator")));
    reps.forEach((m) => list.appendChild(memberRow(m, "Representative")));
    panelBody.appendChild(list);
    const links = el("p", { class: "small" }, [
      el("a", { href: "/bills?sponsor=" + ((sens[0] || {}).id || ""), text: "Bills sponsored by the senator" }),
      document.createTextNode(" · "),
      el("a", { href: "https://leg.wa.gov/legislators/how-to-send-a-message-to-a-legislator/", rel: "noopener", text: "How to contact them ↗" }),
    ]);
    if (sens.length) panelBody.appendChild(links);
  }

  function bbox(geom) {
    let minX = 180, minY = 90, maxX = -180, maxY = -90;
    const walk = (c) => {
      if (typeof c[0] === "number") {
        minX = Math.min(minX, c[0]); maxX = Math.max(maxX, c[0]);
        minY = Math.min(minY, c[1]); maxY = Math.max(maxY, c[1]);
      } else c.forEach(walk);
    };
    walk(geom.coordinates);
    return [[minX, minY], [maxX, maxY]];
  }

  // Ray casting; handles holes and multipolygons.
  function inRing(pt, ring) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [xi, yi] = ring[i], [xj, yj] = ring[j];
      if (yi > pt[1] !== yj > pt[1] && pt[0] < ((xj - xi) * (pt[1] - yi)) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }
  function inPolygon(pt, poly) {
    return inRing(pt, poly[0]) && !poly.slice(1).some((h) => inRing(pt, h));
  }
  function districtAt(pt) {
    return features.find((f) => {
      const g = f.geometry;
      const polys = g.type === "MultiPolygon" ? g.coordinates : [g.coordinates];
      return polys.some((p) => inPolygon(pt, p));
    });
  }

  function select(d, fly) {
    const f = features.find((x) => x.properties.district === Number(d));
    if (!f) return;
    if (map && selected !== null) map.setFeatureState({ source: "districts", id: selected }, { selected: false });
    selected = f.properties.district;
    if (map) {
      map.setFeatureState({ source: "districts", id: selected }, { selected: true });
      if (fly) map.fitBounds(bbox(f.geometry), { padding: 40, maxZoom: 12, duration: 700 });
    }
    showPanel(f);
    document.getElementById("jump").value = String(selected);
    if (location.hash !== "#" + selected) history.replaceState(null, "", "#" + selected);
  }

  document.getElementById("jump").addEventListener("change", (e) => e.target.value && select(e.target.value, true));
  document.querySelectorAll("#district-table a[data-district]").forEach((a) =>
    a.addEventListener("click", (e) => {
      e.preventDefault();
      select(a.dataset.district, true);
      document.getElementById("map").scrollIntoView({ behavior: "smooth", block: "center" });
    })
  );
  document.getElementById("locate").addEventListener("click", () => {
    const btn = document.getElementById("locate");
    if (!navigator.geolocation) { btn.textContent = "Location not available"; return; }
    btn.textContent = "Finding you…";
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const pt = [pos.coords.longitude, pos.coords.latitude];
        const f = districtAt(pt);
        btn.textContent = "Find my district";
        if (f) {
          select(f.properties.district, true);
          if (map) new maplibregl.Marker({ color: "#111" }).setLngLat(pt).addTo(map);
        } else {
          panelTitle.textContent = "Outside Washington";
          panelBody.replaceChildren(el("p", { class: "muted", text: "Your location isn't inside a Washington legislative district." }));
        }
      },
      () => { btn.textContent = "Couldn't get your location"; },
      { enableHighAccuracy: true, timeout: 15000 }
    );
  });

  fetch("/api/districts.geojson")
    .then((r) => r.json())
    .then((data) => {
      features = data.features;
      const fromHash = Number((location.hash || "").replace("#", ""));
      if (fromHash >= 1 && fromHash <= 49) select(fromHash, false);
      window.addEventListener("hashchange", () => {
        const d = Number((location.hash || "").replace("#", ""));
        if (d >= 1 && d <= 49 && d !== selected) select(d, true);
      });
      if (!window.maplibregl || !webglAvailable()) {
        document.getElementById("map").replaceChildren(el("p", { class: "map-fallback", text: "Your browser can't display the interactive map. Every district is listed below." }));
        if (location.hash) select(location.hash.slice(1), false);
        return;
      }
      initMap(data);
    })
    .catch(() => {
      document.getElementById("map").replaceChildren(el("p", { class: "map-fallback", text: "The map couldn't load. Every district is listed below." }));
    });

  function webglAvailable() {
    try {
      const c = document.createElement("canvas");
      return !!(c.getContext("webgl2") || c.getContext("webgl"));
    } catch (e) {
      return false;
    }
  }

  function initMap(data) {
    map = new maplibregl.Map({
      container: "map",
      style: dark ? "https://tiles.openfreemap.org/styles/dark" : "https://tiles.openfreemap.org/styles/positron",
      bounds: WA_BOUNDS,
      fitBoundsOptions: { padding: 16 },
      maxBounds: [[-131, 43.5], [-110, 51]],
      attributionControl: false,
      dragRotate: false,
      pitchWithRotate: false,
    });
    map.touchZoomRotate.disableRotation();
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");
    map.addControl(new maplibregl.AttributionControl({
      compact: true,
      customAttribution: "Districts: WA Office of Financial Management",  // the base map adds its own credits
    }));

    let layersAdded = false;
    let handlersBound = false;
    const fallback = setTimeout(() => {
      if (!layersAdded) map.setStyle(blankStyle());  // base map unavailable: show districts on a plain background
    }, 8000);
    map.on("style.load", () => {
      if (map.getSource("districts")) return;
      layersAdded = true;
      clearTimeout(fallback);
      const labels = {
        type: "FeatureCollection",
        features: data.features.map((f) => ({
          type: "Feature",
          geometry: { type: "Point", coordinates: [f.properties.label_lon, f.properties.label_lat] },
          properties: { district: f.properties.district, senator: f.properties.senator, reps: f.properties.reps },
        })),
      };
      map.addSource("districts", { type: "geojson", data: data, promoteId: "district" });
      map.addSource("district-labels", { type: "geojson", data: labels });
      map.addLayer({
        id: "district-fill", type: "fill", source: "districts",
        paint: {
          "fill-color": ["match", ["get", "mix"], "D", COLORS.D, "R", COLORS.R, "split", COLORS.split, COLORS.vacant],
          "fill-opacity": ["case",
            ["boolean", ["feature-state", "selected"], false], 0.5,
            ["boolean", ["feature-state", "hover"], false], 0.4,
            0.2],
        },
      });
      map.addLayer({
        id: "district-line", type: "line", source: "districts",
        paint: {
          "line-color": dark ? "#dfe5ea" : "#1f2a33",
          "line-width": ["interpolate", ["linear"], ["zoom"], 5, 0.6, 9, 1.2, 13, 2.2],
          "line-opacity": 0.85,
        },
      });
      map.addLayer({
        id: "district-selected", type: "line", source: "districts",
        paint: {
          "line-color": dark ? "#ffffff" : "#0b0f13",
          "line-width": ["case", ["boolean", ["feature-state", "selected"], false], 3.5, 0],
        },
      });
      map.addLayer({
        id: "district-label", type: "symbol", source: "district-labels",
        layout: {
          "text-field": ["step", ["zoom"],
            ["to-string", ["get", "district"]],
            8.5, ["format",
              ["concat", "District ", ["to-string", ["get", "district"]]], { "text-font": ["literal", ["Noto Sans Bold"]], "font-scale": 1.05 },
              "\n", {},
              ["concat", "Sen. ", ["get", "senator"]], { "font-scale": 0.85 },
              "\n", {},
              ["concat", "Reps. ", ["get", "reps"]], { "font-scale": 0.85 }]],
          "text-font": ["Noto Sans Bold"],
          "text-size": ["interpolate", ["linear"], ["zoom"], 5, 11, 8, 13, 11, 14],
          "text-max-width": 16,
          "text-line-height": 1.25,
          "text-padding": 2,
        },
        paint: {
          "text-color": dark ? "#f2f5f7" : "#11181d",
          "text-halo-color": dark ? "rgba(0,0,0,0.85)" : "rgba(255,255,255,0.92)",
          "text-halo-width": 1.6,
        },
      });

      if (selected !== null) select(selected, true);  // highlight a district chosen before the map was ready
      if (handlersBound) return;
      handlersBound = true;
      let hovered = null;
      map.on("mousemove", "district-fill", (e) => {
        map.getCanvas().style.cursor = "pointer";
        const id = e.features[0].id;
        if (hovered !== null && hovered !== id) map.setFeatureState({ source: "districts", id: hovered }, { hover: false });
        hovered = id;
        map.setFeatureState({ source: "districts", id }, { hover: true });
      });
      map.on("mouseleave", "district-fill", () => {
        map.getCanvas().style.cursor = "";
        if (hovered !== null) map.setFeatureState({ source: "districts", id: hovered }, { hover: false });
        hovered = null;
      });
      map.on("click", "district-fill", (e) => select(e.features[0].id, false));

    });
  }

  function blankStyle() {
    return {
      version: 8,
      glyphs: "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf",
      sources: {},
      layers: [{ id: "bg", type: "background", paint: { "background-color": dark ? "#0e1113" : "#eef1f3" } }],
    };
  }
})();
