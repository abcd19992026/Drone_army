import React, { useEffect, useRef, useState, useMemo } from 'react';
import L from 'leaflet';
import {
  Crosshair,
  Navigation2,
  Compass,
  Zap,
  Gauge,
  Maximize2,
  Radio,
  Locate,
  Route,
  Trash2,
} from 'lucide-react';
import { APP_CONFIG } from '../lib/config';
import { formatNumber } from '../lib/utils';
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
  const flightTrailRef = useRef<L.Polyline | null>(null);
  const geofenceCircleRef = useRef<L.Circle | null>(null);
  const userMarkerRef = useRef<L.Marker | null>(null);
  const trailPointsRef = useRef<[number, number][]>([]);

  // State
  const [autoFollow, setAutoFollow] = useState<boolean>(true);
  const [showTrail, setShowTrail] = useState<boolean>(true);
  const [userCoords, setUserCoords] = useState<{ lat: number; lon: number } | null>(null);
  const [trailCount, setTrailCount] = useState<number>(0);

  const homeLat = dock?.lat != null ? Number(dock.lat) : APP_CONFIG.HOME_LAT;
  const homeLon = dock?.lon != null ? Number(dock.lon) : APP_CONFIG.HOME_LON;

  // Numeric coordinate extraction
  const droneLat = drone?.lat != null ? Number(drone.lat) : null;
  const droneLon = drone?.lon != null ? Number(drone.lon) : null;
  const hasValidDroneCoords =
    droneLat != null &&
    droneLon != null &&
    !isNaN(droneLat) &&
    !isNaN(droneLon) &&
    !(droneLat === 0 && droneLon === 0);

  // Calculate live distance from drone to dock
  const distanceToDockMeters = useMemo(() => {
    if (!hasValidDroneCoords) return null;
    const from = L.latLng(droneLat!, droneLon!);
    const to = L.latLng(homeLat, homeLon);
    return from.distanceTo(to);
  }, [hasValidDroneCoords, droneLat, droneLon, homeLat, homeLon]);

  // Calculate live distance to mission target
  const distanceToTargetMeters = useMemo(() => {
    if (
      !hasValidDroneCoords ||
      !activeMission ||
      activeMission.target_lat == null ||
      activeMission.target_lon == null
    ) {
      return null;
    }
    const from = L.latLng(droneLat!, droneLon!);
    const to = L.latLng(Number(activeMission.target_lat), Number(activeMission.target_lon));
    return from.distanceTo(to);
  }, [hasValidDroneCoords, droneLat, droneLon, activeMission]);

  // 1. Initialize Map & Container Resize Handling
  useEffect(() => {
    if (!mapContainerRef.current || mapInstanceRef.current) return;

    const initialCenter: [number, number] = hasValidDroneCoords
      ? [droneLat!, droneLon!]
      : [homeLat, homeLon];

    const map = L.map(mapContainerRef.current, {
      center: initialCenter,
      zoom: 15,
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

    // Detect user manual dragging to pause auto-follow
    map.on('dragstart', () => {
      setAutoFollow(false);
    });

    mapInstanceRef.current = map;

    // Invalidate map size after container mounts
    const timer = setTimeout(() => {
      map.invalidateSize();
    }, 150);

    // Handle container resize
    const resizeObserver = new ResizeObserver(() => {
      map.invalidateSize();
    });
    if (mapContainerRef.current) {
      resizeObserver.observe(mapContainerRef.current);
    }

    // Track user location
    if (navigator.geolocation) {
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          setUserCoords({ lat: pos.coords.latitude, lon: pos.coords.longitude });
        },
        () => {},
        { enableHighAccuracy: true }
      );
    }

    return () => {
      clearTimeout(timer);
      resizeObserver.disconnect();
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
          <div style="position:relative; width:36px; height:36px; display:flex; align-items:center; justify-content:center;">
            <div style="position:absolute; inset:0; border-radius:10px; background:rgba(47,191,113,0.25); border:1.5px solid #2fbf71; box-shadow:0 0 10px rgba(47,191,113,0.4);"></div>
            <span style="font-size:18px; position:relative; z-index:2;">🏠</span>
          </div>
        `,
        iconSize: [36, 36],
        iconAnchor: [18, 18],
      });

      dockMarkerRef.current = L.marker([homeLat, homeLon], { icon: dockIcon })
        .addTo(map)
        .bindPopup(
          `<b>Home Dock (Patna)</b><br>Lat: ${homeLat.toFixed(6)}<br>Lon: ${homeLon.toFixed(6)}`
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
          <div style="position:relative; width:26px; height:26px; display:flex; align-items:center; justify-content:center;">
            <div style="position:absolute; inset:0; border-radius:50%; background:rgba(47,111,237,0.35); animation:ping 2s infinite;"></div>
            <div style="width:12px; height:12px; border-radius:50%; background:#2f6fed; border:2px solid #ffffff; box-shadow:0 0 8px #2f6fed;"></div>
          </div>
        `,
        iconSize: [26, 26],
        iconAnchor: [13, 13],
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

  // 4. Update Drone Marker, Flight Trail, & Auto-Follow Camera
  useEffect(() => {
    const map = mapInstanceRef.current;
    if (!map) return;

    // A. Current Live Drone Position
    if (hasValidDroneCoords) {
      const currentPos: [number, number] = [droneLat!, droneLon!];
      const heading = Number(drone?.heading_deg ?? 0);
      const isPositionValid = drone?.position_valid !== false;

      const ringColor = isPositionValid ? '#00e5ff' : '#e0a51b';
      const ringBg = isPositionValid ? 'rgba(0,229,255,0.3)' : 'rgba(224,165,27,0.3)';

      const droneIcon = L.divIcon({
        className: 'custom-drone-icon',
        html: `
          <div style="position:relative; width:44px; height:44px; display:flex; align-items:center; justify-content:center;">
            <div style="position:absolute; inset:2px; border-radius:50%; background:${ringBg}; border:2px solid ${ringColor}; box-shadow:0 0 16px ${ringColor}; animation:pulse 2s infinite;"></div>
            <div style="position:absolute; inset:8px; border-radius:50%; background:rgba(15,18,22,0.85); border:1px solid ${ringColor};"></div>
            <div style="transform: rotate(${heading}deg); transition: transform 0.3s ease-out; display:flex; align-items:center; justify-content:center; position:relative; z-index:5;">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="${ringColor}" stroke="#ffffff" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                <polygon points="12 2 19 21 12 17 5 21 12 2"></polygon>
              </svg>
            </div>
          </div>
        `,
        iconSize: [44, 44],
        iconAnchor: [22, 22],
      });

      if (!droneMarkerRef.current) {
        droneMarkerRef.current = L.marker(currentPos, {
          icon: droneIcon,
          zIndexOffset: 2000,
        }).addTo(map);
      } else {
        droneMarkerRef.current.setIcon(droneIcon);
        droneMarkerRef.current.setLatLng(currentPos);
      }

      droneMarkerRef.current.bindPopup(
        `<div style="font-family: monospace; font-size: 11px; line-height: 1.5;">
          <b style="color: #00e5ff; font-size: 12px;">${drone?.name || 'Hexacopter-1'}</b><br/>
          <span>Fix: <b>${isPositionValid ? '3D RTK Fix' : 'Degraded Fix'}</b></span><br/>
          <span>Lat: <b>${droneLat!.toFixed(6)}</b></span><br/>
          <span>Lon: <b>${droneLon!.toFixed(6)}</b></span><br/>
          <span>Alt: <b>${drone?.alt_m_relative?.toFixed(1) || '0'} m rel</b> (${drone?.alt_m_amsl?.toFixed(1) || '0'} m AMSL)</span><br/>
          <span>Speed: <b>${drone?.groundspeed_ms?.toFixed(1) || '0'} m/s</b> (${((drone?.groundspeed_ms ?? 0) * 3.6).toFixed(1)} km/h)</span><br/>
          <span>Heading: <b>${Math.round(heading)}°</b></span><br/>
          <span>Mode: <b>${drone?.mode || '—'}</b> (${drone?.control_mode || 'AUTO'})</span>
        </div>`
      );

      // B. Append to Flight Breadcrumb Trail
      if (showTrail) {
        const history = trailPointsRef.current;
        const last = history[history.length - 1];
        // Only append if moved > 0.8 meters to avoid redundant vertices
        if (!last || map.distance(last, currentPos) >= 0.8) {
          history.push(currentPos);
          if (history.length > 500) {
            history.shift();
          }
          setTrailCount(history.length);
        }

        if (!flightTrailRef.current) {
          flightTrailRef.current = L.polyline(history, {
            color: '#00e5ff',
            weight: 3.5,
            opacity: 0.85,
            lineJoin: 'round',
            lineCap: 'round',
          }).addTo(map);
        } else {
          flightTrailRef.current.setLatLngs(history);
        }
      }

      // C. Auto-Follow Camera Panning
      if (autoFollow) {
        map.panTo(currentPos, { animate: true, duration: 0.4 });
      }

      // Remove last known ghost marker since current fix is active
      if (lastKnownMarkerRef.current) {
        map.removeLayer(lastKnownMarkerRef.current);
        lastKnownMarkerRef.current = null;
      }
    } else {
      // If no valid current coords, remove drone marker
      if (droneMarkerRef.current) {
        map.removeLayer(droneMarkerRef.current);
        droneMarkerRef.current = null;
      }

      // Fallback: Last Trustworthy Fix Ghost Marker
      const lLat = drone?.last_known_lat != null ? Number(drone.last_known_lat) : null;
      const lLon = drone?.last_known_lon != null ? Number(drone.last_known_lon) : null;

      if (lLat != null && lLon != null && !isNaN(lLat) && !isNaN(lLon) && !(lLat === 0 && lLon === 0)) {
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
          lastKnownMarkerRef.current = L.marker([lLat, lLon], { icon: lastKnownIcon })
            .addTo(map)
            .bindPopup(
              `<b>Last Trustworthy Fix</b><br>Lat: ${lLat.toFixed(6)}<br>Lon: ${lLon.toFixed(6)}<br>Alt: ${drone?.last_known_alt_m?.toFixed(1) || '—'} m`
            );
        } else {
          lastKnownMarkerRef.current.setLatLng([lLat, lLon]);
        }
      } else if (lastKnownMarkerRef.current) {
        map.removeLayer(lastKnownMarkerRef.current);
        lastKnownMarkerRef.current = null;
      }
    }
  }, [hasValidDroneCoords, droneLat, droneLon, drone, autoFollow, showTrail]);

  // 5. Update Active Mission Target & Waypoint Vector Line
  useEffect(() => {
    const map = mapInstanceRef.current;
    if (!map) return;

    const tLat = activeMission?.target_lat != null ? Number(activeMission.target_lat) : null;
    const tLon = activeMission?.target_lon != null ? Number(activeMission.target_lon) : null;

    if (tLat != null && tLon != null && !isNaN(tLat) && !isNaN(tLon)) {
      const targetPos: [number, number] = [tLat, tLon];

      // Target marker
      const targetIcon = L.divIcon({
        className: 'custom-target-icon',
        html: `
          <div style="position:relative; width:32px; height:32px; display:flex; align-items:center; justify-content:center;">
            <div style="position:absolute; inset:0; border-radius:50%; background:rgba(224,83,61,0.25); border:2px solid #e0533d; animation:ping 1.5s infinite;"></div>
            <div style="width:12px; height:12px; border-radius:50%; background:#e0533d; box-shadow:0 0 10px #e0533d;"></div>
          </div>
        `,
        iconSize: [32, 32],
        iconAnchor: [16, 16],
      });

      if (!targetMarkerRef.current) {
        targetMarkerRef.current = L.marker(targetPos, { icon: targetIcon })
          .addTo(map)
          .bindPopup(`<b>Mission Target Waypoint</b><br>Type: ${activeMission?.type?.toUpperCase()}<br>Lat: ${tLat.toFixed(6)}<br>Lon: ${tLon.toFixed(6)}`);
      } else {
        targetMarkerRef.current.setLatLng(targetPos);
      }

      // Connecting corridor line from current drone position (or dock) to target
      const startPos: [number, number] = hasValidDroneCoords
        ? [droneLat!, droneLon!]
        : [homeLat, homeLon];

      if (!targetLineRef.current) {
        targetLineRef.current = L.polyline([startPos, targetPos], {
          color: '#e0533d',
          weight: 2.5,
          dashArray: '6, 8',
          opacity: 0.85,
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
  }, [activeMission, hasValidDroneCoords, droneLat, droneLon, homeLat, homeLon]);

  // Center / Framing Handlers
  const handleCenterOnDrone = () => {
    const map = mapInstanceRef.current;
    if (!map) return;
    setAutoFollow(true);
    if (hasValidDroneCoords) {
      map.setView([droneLat!, droneLon!], 16, { animate: true });
    } else {
      map.setView([homeLat, homeLon], 15, { animate: true });
    }
  };

  const handleCenterOnUser = () => {
    const map = mapInstanceRef.current;
    if (!map) return;
    setAutoFollow(false);
    if (userCoords) {
      map.setView([userCoords.lat, userCoords.lon], 16, { animate: true });
    } else if (navigator.geolocation) {
      navigator.geolocation.getCurrentPosition((pos) => {
        const coords = { lat: pos.coords.latitude, lon: pos.coords.longitude };
        setUserCoords(coords);
        map.setView([coords.lat, coords.lon], 16, { animate: true });
      });
    }
  };

  const handleFitAll = () => {
    const map = mapInstanceRef.current;
    if (!map) return;
    setAutoFollow(false);

    const points: [number, number][] = [[homeLat, homeLon]];
    if (hasValidDroneCoords) {
      points.push([droneLat!, droneLon!]);
    }
    if (
      activeMission &&
      activeMission.target_lat != null &&
      activeMission.target_lon != null
    ) {
      points.push([Number(activeMission.target_lat), Number(activeMission.target_lon)]);
    }
    if (userCoords) {
      points.push([userCoords.lat, userCoords.lon]);
    }

    if (points.length === 1) {
      map.setView(points[0], 15, { animate: true });
    } else {
      const bounds = L.latLngBounds(points);
      map.fitBounds(bounds, { padding: [60, 60], maxZoom: 16, animate: true });
    }
  };

  const handleClearTrail = () => {
    const map = mapInstanceRef.current;
    trailPointsRef.current = [];
    setTrailCount(0);
    if (flightTrailRef.current && map) {
      map.removeLayer(flightTrailRef.current);
      flightTrailRef.current = null;
    }
  };

  const formatDistanceLabel = (meters: number | null) => {
    if (meters == null || isNaN(meters)) return '—';
    if (meters < 1000) return `${Math.round(meters)} m`;
    return `${(meters / 1000).toFixed(2)} km`;
  };

  return (
    <div className="relative w-full h-[calc(100vh-140px)] min-h-[500px] rounded-2xl overflow-hidden border border-bg-line shadow-2xl">
      {/* Map Leaflet Container */}
      <div ref={mapContainerRef} className="w-full h-full z-0 bg-bg" />

      {/* Top Left: Tracking Mode Badge & Staleness Banner */}
      <div className="absolute top-4 left-4 z-10 flex flex-col gap-2 max-w-sm sm:max-w-md">
        {isStale && (
          <div className="bg-tactical-bad/90 backdrop-blur text-white px-3.5 py-2 rounded-xl text-xs font-bold flex items-center gap-2 shadow-lg animate-pulse-urgent border border-tactical-bad">
            <span className="w-2.5 h-2.5 rounded-full bg-white animate-ping shrink-0" />
            <span>MAP DATA STALE: Telemetry not current</span>
          </div>
        )}

        {/* Auto-Follow Camera Indicator */}
        <div className="flex items-center gap-2">
          {autoFollow ? (
            <div className="bg-bg-card/90 backdrop-blur border border-tactical-cyan/40 px-3 py-1.5 rounded-xl shadow-xl flex items-center gap-2 text-xs font-mono text-tactical-cyan">
              <span className="w-2 h-2 rounded-full bg-tactical-cyan animate-pulse shrink-0" />
              <span className="font-bold">AUTO-FOLLOW ON</span>
              <span className="text-gray-400 text-[10px] hidden sm:inline">(Tracking Drone)</span>
            </div>
          ) : (
            <button
              onClick={handleCenterOnDrone}
              className="bg-tactical-warn/20 hover:bg-tactical-warn/30 border border-tactical-warn text-tactical-warn px-3 py-1.5 rounded-xl shadow-xl flex items-center gap-2 text-xs font-mono font-bold transition-all animate-pulse"
              title="Click to resume camera lock on drone"
            >
              <Locate className="w-3.5 h-3.5 shrink-0" />
              <span>FREE CAMERA (CLICK TO RESUME LOCK)</span>
            </button>
          )}

          {trailCount > 0 && (
            <div className="bg-bg-card/85 backdrop-blur border border-bg-line px-2.5 py-1.5 rounded-xl text-[11px] font-mono text-gray-300 hidden md:flex items-center gap-1.5">
              <Route className="w-3 h-3 text-tactical-cyan" />
              <span>Trail: {trailCount} pts</span>
            </div>
          )}
        </div>
      </div>

      {/* Top Right: Live Telemetry & Radar HUD */}
      <div className="absolute top-4 right-4 z-10 hidden sm:flex flex-col gap-2.5 bg-bg-card/95 backdrop-blur border border-bg-line p-3.5 rounded-2xl shadow-2xl text-xs font-mono max-w-[280px] w-full">
        <div className="flex items-center justify-between pb-2 border-b border-bg-line">
          <div className="flex items-center gap-1.5 text-tactical-cyan font-bold">
            <Radio className="w-4 h-4 animate-pulse" />
            <span>LIVE FLIGHT RADAR</span>
          </div>
          <span className="text-[10px] text-gray-400">
            {drone?.status?.toUpperCase() || 'OFFLINE'}
          </span>
        </div>

        {/* Live GPS Coordinates */}
        <div className="space-y-1">
          <div className="text-[10px] text-gray-400 uppercase tracking-wider flex items-center justify-between">
            <span>GPS Position</span>
            <span className={drone?.position_valid ? 'text-tactical-ok' : 'text-tactical-warn'}>
              {drone?.position_valid ? '3D FIX' : 'DEGRADED'}
            </span>
          </div>
          <div className="text-xs font-bold text-tactical-cyan font-mono truncate">
            {hasValidDroneCoords
              ? `${droneLat!.toFixed(6)}, ${droneLon!.toFixed(6)}`
              : 'Acquiring GPS Coords...'}
          </div>
        </div>

        {/* Dynamics Bar */}
        <div className="grid grid-cols-3 gap-1.5 pt-1 text-center">
          <div className="bg-bg-input p-1.5 rounded-lg border border-bg-line">
            <div className="text-[9px] text-gray-400 uppercase flex items-center justify-center gap-0.5">
              <Gauge className="w-2.5 h-2.5 text-tactical-cyan" /> Alt
            </div>
            <div className="font-bold text-white text-[11px] mt-0.5">
              {formatNumber(drone?.alt_m_relative, 1, 'm')}
            </div>
          </div>

          <div className="bg-bg-input p-1.5 rounded-lg border border-bg-line">
            <div className="text-[9px] text-gray-400 uppercase flex items-center justify-center gap-0.5">
              <Zap className="w-2.5 h-2.5 text-tactical-warn" /> Spd
            </div>
            <div className="font-bold text-white text-[11px] mt-0.5">
              {formatNumber(drone?.groundspeed_ms, 1, 'm/s')}
            </div>
          </div>

          <div className="bg-bg-input p-1.5 rounded-lg border border-bg-line">
            <div className="text-[9px] text-gray-400 uppercase flex items-center justify-center gap-0.5">
              <Compass className="w-2.5 h-2.5 text-tactical-ok" /> Hdg
            </div>
            <div className="font-bold text-white text-[11px] mt-0.5">
              {drone?.heading_deg != null ? `${Math.round(drone.heading_deg)}°` : '—'}
            </div>
          </div>
        </div>

        {/* Distances */}
        <div className="space-y-1 pt-1 text-[11px]">
          <div className="flex items-center justify-between text-gray-300">
            <span className="text-gray-400">Distance to Dock:</span>
            <span className="font-bold text-white">{formatDistanceLabel(distanceToDockMeters)}</span>
          </div>

          {activeMission && activeMission.target_lat != null && (
            <div className="flex items-center justify-between text-tactical-bad">
              <span className="text-gray-400">Target Range:</span>
              <span className="font-bold">{formatDistanceLabel(distanceToTargetMeters)}</span>
            </div>
          )}

          <div className="flex items-center justify-between text-gray-400">
            <span>Satellites / Fix:</span>
            <span className="text-gray-200">
              {drone?.gps_satellites ?? 0} sats (Fix {drone?.gps_fix_type ?? 0})
            </span>
          </div>
        </div>

        {/* Layer Controls */}
        <div className="pt-2 border-t border-bg-line flex items-center justify-between text-[11px]">
          <button
            onClick={() => setShowTrail(!showTrail)}
            className={`flex items-center gap-1 transition-colors ${
              showTrail ? 'text-tactical-cyan' : 'text-gray-500'
            }`}
          >
            <Route className="w-3.5 h-3.5" />
            <span>Trail {showTrail ? 'ON' : 'OFF'}</span>
          </button>

          {trailCount > 0 && (
            <button
              onClick={handleClearTrail}
              className="text-gray-400 hover:text-tactical-bad flex items-center gap-1 transition-colors"
              title="Clear flight trail"
            >
              <Trash2 className="w-3 h-3" />
              <span>Clear</span>
            </button>
          )}
        </div>
      </div>

      {/* Bottom Left: Tactical Interactive Map Controls */}
      <div className="absolute bottom-6 left-4 z-10 flex flex-col gap-2">
        {/* Center / Follow Drone */}
        <button
          onClick={handleCenterOnDrone}
          className={`p-3 rounded-xl border shadow-xl transition-all active:scale-95 flex items-center gap-2 text-xs font-bold ${
            autoFollow
              ? 'bg-tactical-cyan text-black border-tactical-cyan shadow-tactical-cyan/20'
              : 'bg-bg-card/90 hover:bg-bg-card border-bg-line text-tactical-cyan'
          }`}
          title="Center and follow drone"
        >
          <Navigation2 className="w-4 h-4" />
          <span className="hidden sm:inline">
            {autoFollow ? 'Tracking Drone' : 'Center Drone'}
          </span>
        </button>

        {/* Fit All Corridor View */}
        <button
          onClick={handleFitAll}
          className="p-3 rounded-xl bg-bg-card/90 hover:bg-bg-card border border-bg-line text-gray-200 hover:text-white shadow-xl transition-all active:scale-95 flex items-center gap-2 text-xs font-bold"
          title="Fit full operational corridor (Drone, Dock, Target, Operator)"
        >
          <Maximize2 className="w-4 h-4 text-tactical-cyan" />
          <span className="hidden sm:inline">Fit All View</span>
        </button>

        {/* My Geolocation */}
        <button
          onClick={handleCenterOnUser}
          className="p-3 rounded-xl bg-bg-card/90 hover:bg-bg-card border border-bg-line text-tactical-blue shadow-xl transition-all active:scale-95 flex items-center gap-2 text-xs font-bold"
          title="Center map on Operator Location"
        >
          <Crosshair className="w-4 h-4" />
          <span className="hidden sm:inline">My Location</span>
        </button>
      </div>
    </div>
  );
};
