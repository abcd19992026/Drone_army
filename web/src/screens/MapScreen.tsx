import React, { useEffect, useRef, useState } from 'react';
import L from 'leaflet';
import { Crosshair, Navigation2, Layers } from 'lucide-react';
import { APP_CONFIG } from '../lib/config';
import type { DroneRow, DockRow, MissionRow } from '../types';

interface MapScreenProps {
  drone: DroneRow | null;
  dock: DockRow | null;
  activeMission: MissionRow | null;
  isStale: boolean;
}

// Custom Stepped Scale Control:
// - Below 1 km: +50m increments (50m, 100m, 150m, 200m... 950m)
// - 1 km & above: +500m increments (1 km, 1.5 km, 2 km, 2.5 km... 6 km)
class CustomSteppedScale extends L.Control {
  private _line: HTMLElement | null = null;

  constructor(options?: L.ControlOptions) {
    super({ position: 'bottomright', ...options });
  }

  onAdd(map: L.Map) {
    const className = 'leaflet-control-scale';
    const container = L.DomUtil.create('div', className);
    this._line = L.DomUtil.create('div', className + '-line', container);

    map.on('move', this._update, this);
    map.on('zoomend', this._update, this);
    map.whenReady(this._update, this);

    return container;
  }

  onRemove(map: L.Map) {
    map.off('move', this._update, this);
    map.off('zoomend', this._update, this);
  }

  private _update = () => {
    const map = (this as any)._map as L.Map | undefined;
    if (!map || !this._line) return;

    const y = map.getSize().y / 2;
    const maxMeters = map.distance(
      map.containerPointToLatLng([0, y]),
      map.containerPointToLatLng([120, y])
    );
    if (!maxMeters || isNaN(maxMeters)) return;

    let dist = 50;
    if (maxMeters < 50) {
      dist = 50;
    } else if (maxMeters < 1000) {
      dist = Math.floor(maxMeters / 50) * 50;
      if (dist === 0) dist = 50;
    } else {
      dist = Math.floor(maxMeters / 500) * 500;
      if (dist === 0) dist = 1000;
    }

    let text = '';
    if (dist < 1000) {
      text = `${dist} m`;
    } else {
      const km = dist / 1000;
      text = km % 1 === 0 ? `${km} km` : `${km.toFixed(1)} km`;
    }

    this._line.innerHTML = text;
  };
}

export const MapScreen: React.FC<MapScreenProps> = ({
  drone,
  dock,
  activeMission,
  isStale,
}) => {
  const mapContainerRef = useRef<HTMLDivElement>(null);
  const mapInstanceRef = useRef<L.Map | null>(null);

  // Markers & Layers refs
  const droneMarkerRef = useRef<L.Marker | null>(null);
  const lastKnownMarkerRef = useRef<L.Marker | null>(null);
  const dockMarkerRef = useRef<L.Marker | null>(null);
  const targetMarkerRef = useRef<L.Marker | null>(null);
  const targetLineRef = useRef<L.Polyline | null>(null);
  const geofenceCircleRef = useRef<L.Circle | null>(null);
  const userMarkerRef = useRef<L.Marker | null>(null);

  const [userCoords, setUserCoords] = useState<{ lat: number; lon: number } | null>(null);

  const homeLat = dock?.lat ?? APP_CONFIG.HOME_LAT;
  const homeLon = dock?.lon ?? APP_CONFIG.HOME_LON;

  // 1. Initialize Map
  useEffect(() => {
    if (!mapContainerRef.current || mapInstanceRef.current) return;

    const initialCenter: [number, number] =
      drone?.position_valid && drone.lat != null && drone.lon != null
        ? [drone.lat, drone.lon]
        : [homeLat, homeLon];

    const map = L.map(mapContainerRef.current, {
      center: initialCenter,
      zoom: 14,
      zoomControl: false,
    });

    // OpenStreetMap tiles (100% keyless, styled dark via CSS filter)
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19,
    }).addTo(map);

    // Zoom & Custom Stepped Scale control in bottom right
    L.control.zoom({ position: 'bottomright' }).addTo(map);
    new CustomSteppedScale().addTo(map);

    // Geofence Circle (6000m radius around Home Dock)
    const geofence = L.circle([homeLat, homeLon], {
      radius: APP_CONFIG.MAX_RADIUS_M,
      color: '#00e5ff',
      weight: 1.5,
      dashArray: '6, 6',
      fillColor: '#00e5ff',
      fillOpacity: 0.04,
    }).addTo(map);
    geofenceCircleRef.current = geofence;

    mapInstanceRef.current = map;

    // Track user location
    if (navigator.geolocation) {
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          setUserCoords({ lat: pos.coords.latitude, lon: pos.coords.longitude });
        },
        () => { },
        { enableHighAccuracy: true }
      );
    }

    return () => {
      map.remove();
      mapInstanceRef.current = null;
    };
  }, [homeLat, homeLon]);

  // 2. Update Dock Marker
  useEffect(() => {
    const map = mapInstanceRef.current;
    if (!map) return;

    if (!dockMarkerRef.current) {
      const dockIcon = L.divIcon({
        className: 'custom-dock-icon',
        html: `
          <div style="position:relative; width:34px; height:34px; display:flex; align-items:center; justify-content:center;">
            <div style="position:absolute; inset:0; border-radius:8px; background:rgba(47,191,113,0.25); border:1.5px solid #2fbf71;"></div>
            <span style="font-size:16px; position:relative; z-index:2;">🏠</span>
          </div>
        `,
        iconSize: [34, 34],
        iconAnchor: [17, 17],
      });

      dockMarkerRef.current = L.marker([homeLat, homeLon], { icon: dockIcon })
        .addTo(map)
        .bindPopup(
          `<b>Home Dock (Agamkuan)</b><br>Lat: ${homeLat.toFixed(6)}<br>Lon: ${homeLon.toFixed(6)}`
        );
    } else {
      dockMarkerRef.current.setLatLng([homeLat, homeLon]);
    }
  }, [homeLat, homeLon]);

  // 3. Update User Geolocation Marker
  useEffect(() => {
    const map = mapInstanceRef.current;
    if (!map || !userCoords) return;

    if (!userMarkerRef.current) {
      const userIcon = L.divIcon({
        className: 'custom-user-icon',
        html: `
          <div style="position:relative; width:24px; height:24px; display:flex; align-items:center; justify-content:center;">
            <div style="position:absolute; inset:0; border-radius:50%; background:rgba(47,111,237,0.3); animation:ping 2s infinite;"></div>
            <div style="width:12px; height:12px; border-radius:50%; background:#2f6fed; border:2px solid #ffffff;"></div>
          </div>
        `,
        iconSize: [24, 24],
        iconAnchor: [12, 12],
      });

      userMarkerRef.current = L.marker([userCoords.lat, userCoords.lon], {
        icon: userIcon,
      })
        .addTo(map)
        .bindPopup('<b>Operator Current Location</b>');
    } else {
      userMarkerRef.current.setLatLng([userCoords.lat, userCoords.lon]);
    }
  }, [userCoords]);

  // 4. Update Drone & Last Known Position Marker
  useEffect(() => {
    const map = mapInstanceRef.current;
    if (!map) return;

    // A. Current Drone Position
    if (drone?.position_valid && drone.lat != null && drone.lon != null) {
      const heading = drone.heading_deg ?? 0;
      const droneIcon = L.divIcon({
        className: 'custom-drone-icon',
        html: `
          <div style="position:relative; width:40px; height:40px; display:flex; align-items:center; justify-content:center;">
            <div style="position:absolute; inset:2px; border-radius:50%; background:rgba(0,229,255,0.25); border:1.5px solid #00e5ff; box-shadow:0 0 15px #00e5ff;"></div>
            <div style="transform: rotate(${heading}deg); transition: transform 0.4s ease-out; display:flex; align-items:center; justify-content:center;">
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#ffffff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                <polygon points="12 2 19 21 12 17 5 21 12 2"></polygon>
              </svg>
            </div>
          </div>
        `,
        iconSize: [40, 40],
        iconAnchor: [20, 20],
      });

      if (!droneMarkerRef.current) {
        droneMarkerRef.current = L.marker([drone.lat, drone.lon], {
          icon: droneIcon,
          zIndexOffset: 1000,
        }).addTo(map);
      } else {
        droneMarkerRef.current.setIcon(droneIcon);
        droneMarkerRef.current.setLatLng([drone.lat, drone.lon]);
      }

      droneMarkerRef.current.bindPopup(
        `<b>${drone.name || 'Drone'}</b><br>Mode: ${drone.mode || '—'}<br>Alt: ${drone.alt_m_relative?.toFixed(1) || '0'} m<br>Speed: ${drone.groundspeed_ms?.toFixed(1) || '0'} m/s`
      );
    } else if (droneMarkerRef.current) {
      map.removeLayer(droneMarkerRef.current);
      droneMarkerRef.current = null;
    }

    // B. Last Known Position (Visually Distinct Ghost Marker)
    if (
      drone?.last_known_lat != null &&
      drone?.last_known_lon != null &&
      (!drone?.position_valid || droneMarkerRef.current === null)
    ) {
      const lastKnownIcon = L.divIcon({
        className: 'custom-lastknown-icon',
        html: `
          <div style="position:relative; width:36px; height:36px; display:flex; align-items:center; justify-content:center;">
            <div style="position:absolute; inset:0; border-radius:50%; border:2px dashed #e0a51b; background:rgba(224,165,27,0.15);"></div>
            <span style="font-size:14px; color:#e0a51b; font-weight:bold;">?</span>
          </div>
        `,
        iconSize: [36, 36],
        iconAnchor: [18, 18],
      });

      if (!lastKnownMarkerRef.current) {
        lastKnownMarkerRef.current = L.marker(
          [drone.last_known_lat, drone.last_known_lon],
          { icon: lastKnownIcon }
        )
          .addTo(map)
          .bindPopup(
            `<b>Last Trustworthy Fix</b><br>Lat: ${drone.last_known_lat.toFixed(6)}<br>Lon: ${drone.last_known_lon.toFixed(6)}<br>Alt: ${drone.last_known_alt_m?.toFixed(1) || '—'} m`
          );
      } else {
        lastKnownMarkerRef.current.setLatLng([
          drone.last_known_lat,
          drone.last_known_lon,
        ]);
      }
    } else if (lastKnownMarkerRef.current) {
      map.removeLayer(lastKnownMarkerRef.current);
      lastKnownMarkerRef.current = null;
    }
  }, [drone]);

  // 5. Update Active Mission Target & Vector Line
  useEffect(() => {
    const map = mapInstanceRef.current;
    if (!map) return;

    if (
      activeMission &&
      activeMission.target_lat != null &&
      activeMission.target_lon != null
    ) {
      const targetPos: [number, number] = [
        activeMission.target_lat,
        activeMission.target_lon,
      ];

      // Target marker
      const targetIcon = L.divIcon({
        className: 'custom-target-icon',
        html: `
          <div style="position:relative; width:30px; height:30px; display:flex; align-items:center; justify-content:center;">
            <div style="position:absolute; inset:0; border-radius:50%; background:rgba(224,83,61,0.25); border:2px solid #e0533d; animation:ping 1.5s infinite;"></div>
            <div style="width:10px; height:10px; border-radius:50%; background:#e0533d;"></div>
          </div>
        `,
        iconSize: [30, 30],
        iconAnchor: [15, 15],
      });

      if (!targetMarkerRef.current) {
        targetMarkerRef.current = L.marker(targetPos, { icon: targetIcon })
          .addTo(map)
          .bindPopup(`<b>Mission Target Waypoint</b><br>Type: ${activeMission.type}`);
      } else {
        targetMarkerRef.current.setLatLng(targetPos);
      }

      // Connecting line from current position or dock
      const startPos: [number, number] =
        drone?.lat != null && drone?.lon != null
          ? [drone.lat, drone.lon]
          : [homeLat, homeLon];

      if (!targetLineRef.current) {
        targetLineRef.current = L.polyline([startPos, targetPos], {
          color: '#00e5ff',
          weight: 2.5,
          dashArray: '8, 8',
          opacity: 0.8,
        }).addTo(map);
      } else {
        targetLineRef.current.setLatLngs([startPos, targetPos]);
      }
    } else {
      if (targetMarkerRef.current) {
        map.removeLayer(targetMarkerRef.current);
        targetMarkerRef.current = null;
      }
      if (targetLineRef.current) {
        map.removeLayer(targetLineRef.current);
        targetLineRef.current = null;
      }
    }
  }, [activeMission, drone, homeLat, homeLon]);

  const handleCenterOnDrone = () => {
    const map = mapInstanceRef.current;
    if (!map) return;
    if (drone?.lat != null && drone?.lon != null) {
      map.setView([drone.lat, drone.lon], 16, { animate: true });
    } else {
      map.setView([homeLat, homeLon], 15, { animate: true });
    }
  };

  const handleCenterOnUser = () => {
    const map = mapInstanceRef.current;
    if (!map) return;
    if (userCoords) {
      map.setView([userCoords.lat, userCoords.lon], 16, { animate: true });
    } else {
      navigator.geolocation?.getCurrentPosition((pos) => {
        const coords = { lat: pos.coords.latitude, lon: pos.coords.longitude };
        setUserCoords(coords);
        map.setView([coords.lat, coords.lon], 16, { animate: true });
      });
    }
  };

  return (
    <div className="relative w-full h-[calc(100vh-140px)] min-h-[500px] rounded-2xl overflow-hidden border border-bg-line shadow-2xl">
      {/* Map Container */}
      <div ref={mapContainerRef} className="w-full h-full z-0 bg-bg" />

      {/* Staleness floating warning on map */}
      {isStale && (
        <div className="absolute top-4 left-4 right-4 sm:right-auto z-10 bg-tactical-bad/90 backdrop-blur text-white px-3.5 py-2 rounded-xl text-xs font-bold flex items-center gap-2 shadow-lg animate-pulse-urgent">
          <span className="w-2 h-2 rounded-full bg-white animate-ping" />
          MAP DATA STALE: Telemetry not current
        </div>
      )}

      {/* Floating HUD Widget */}
      <div className="absolute top-4 right-4 z-10 hidden sm:flex flex-col gap-2 bg-bg-card/90 backdrop-blur border border-bg-line p-3 rounded-xl shadow-xl text-xs font-mono">
        <div className="flex items-center gap-2 text-tactical-cyan font-bold">
          <Layers className="w-4 h-4" />
          <span>RADAR LAYERS</span>
        </div>
        <div className="flex items-center gap-2 text-gray-300">
          <span className="w-3 h-3 rounded bg-tactical-ok/20 border border-tactical-ok" />
          <span>Home Dock (Patna)</span>
        </div>
        <div className="flex items-center gap-2 text-gray-300">
          <span className="w-3 h-3 rounded-full bg-tactical-cyan/20 border border-tactical-cyan" />
          <span>Drone (Heading: {drone?.heading_deg != null ? `${Math.round(drone.heading_deg)}°` : '—'})</span>
        </div>
        <div className="flex items-center gap-2 text-gray-300">
          <span className="w-3 h-3 rounded-full border border-dashed border-tactical-cyan" />
          <span>6 km Geofence Ring</span>
        </div>
        {activeMission && (
          <div className="flex items-center gap-2 text-tactical-bad font-bold">
            <span className="w-3 h-3 rounded-full bg-tactical-bad" />
            <span>Target ({activeMission.type})</span>
          </div>
        )}
      </div>

      {/* Center Controls (Bottom Left) */}
      <div className="absolute bottom-6 left-4 z-10 flex flex-col gap-2">
        <button
          onClick={handleCenterOnDrone}
          className="p-3 rounded-xl bg-bg-card/90 hover:bg-bg-card border border-bg-line text-tactical-cyan shadow-xl transition-all active:scale-95 flex items-center gap-2 text-xs font-bold"
          title="Center map on Drone"
        >
          <Navigation2 className="w-4 h-4" />
          <span className="hidden sm:inline">Center Drone</span>
        </button>

        <button
          onClick={handleCenterOnUser}
          className="p-3 rounded-xl bg-bg-card/90 hover:bg-bg-card border border-bg-line text-tactical-blue shadow-xl transition-all active:scale-95 flex items-center gap-2 text-xs font-bold"
          title="Center map on Operator Geolocation"
        >
          <Crosshair className="w-4 h-4" />
          <span className="hidden sm:inline">My Location</span>
        </button>
      </div>
    </div>
  );
};
