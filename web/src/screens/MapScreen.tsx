import React, { useEffect, useRef, useState, useMemo, useCallback } from 'react';
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
  Target,
  X,
  Send,
  CheckCircle2,
  AlertTriangle,
  Plane,
} from 'lucide-react';
import { APP_CONFIG } from '../lib/config';
import { formatNumber } from '../lib/utils';
import type { DroneRow, DockRow, MissionRow, CommandType } from '../types';

interface MapScreenProps {
  drone: DroneRow | null;
  dock: DockRow | null;
  activeMission: MissionRow | null;
  isStale: boolean;
  onIssueCommand?: (
    type: CommandType,
    extraParams?: {
      target_lat?: number;
      target_lon?: number;
      target_alt_m?: number;
      params?: Record<string, any>;
      mission_id?: string;
    }
  ) => Promise<{ success: boolean; error?: string }>;
  isSending?: boolean;
}

function computeHaversineKm(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const R = 6371; // Earth's radius in km
  const dLat = ((lat2 - lat1) * Math.PI) / 180;
  const dLon = ((lon2 - lon1) * Math.PI) / 180;
  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos((lat1 * Math.PI) / 180) *
      Math.cos((lat2 * Math.PI) / 180) *
      Math.sin(dLon / 2) *
      Math.sin(dLon / 2);
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  return R * c;
}

function getCompassDirection(deg: number): string {
  const directions = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
  const index = Math.round(((deg % 360) + 360) % 360 / 45) % 8;
  return directions[index];
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
  onIssueCommand,
  isSending = false,
}) => {
  const mapContainerRef = useRef<HTMLDivElement>(null);
  const [mapInstance, setMapInstance] = useState<L.Map | null>(null);
  const mapInstanceRef = useRef<L.Map | null>(null);

  // Markers & Layers refs
  const droneMarkerRef = useRef<L.Marker | null>(null);
  const dockMarkerRef = useRef<L.Marker | null>(null);
  const targetMarkerRef = useRef<L.Marker | null>(null);
  const targetLineRef = useRef<L.Polyline | null>(null);
  const flightTrailRef = useRef<L.Polyline | null>(null);
  const geofenceCircleRef = useRef<L.Circle | null>(null);
  const userMarkerRef = useRef<L.Marker | null>(null);
  const trailPointsRef = useRef<[number, number][]>([]);

  // Picking Target Marker ref & flag
  const pickingMarkerRef = useRef<L.Marker | null>(null);
  const isPickingRef = useRef<boolean>(false);

  // State
  const [autoFollow, setAutoFollow] = useState<boolean>(true);
  const [showTrail, setShowTrail] = useState<boolean>(true);
  const [userCoords, setUserCoords] = useState<{ lat: number; lon: number } | null>(null);
  const [trailCount, setTrailCount] = useState<number>(0);
  const [isDroneInView, setIsDroneInView] = useState<boolean>(true);

  // Set Target Location Picker State
  const [isPickingMode, setIsPickingMode] = useState<boolean>(false);
  const [pickedLat, setPickedLat] = useState<number | ''>('');
  const [pickedLon, setPickedLon] = useState<number | ''>('');
  const [pickingError, setPickingError] = useState<string | null>(null);
  const [pickingSuccess, setPickingSuccess] = useState<string | null>(null);
  const [isSendingLocal, setIsSendingLocal] = useState<boolean>(false);

  // Lifecycle refs
  const initialCenteredRef = useRef<boolean>(false);
  const prevAirborneRef = useRef<boolean>(false);
  const cumulativeHeadingRef = useRef<number>(0);
  const lastIconStateRef = useRef<string>('');

  const homeLat = dock?.lat != null ? Number(dock.lat) : APP_CONFIG.HOME_LAT;
  const homeLon = dock?.lon != null ? Number(dock.lon) : APP_CONFIG.HOME_LON;

  // Numeric coordinate extraction with guaranteed fallback (live -> last_known -> home dock)
  const liveLat = drone?.lat != null ? Number(drone.lat) : null;
  const liveLon = drone?.lon != null ? Number(drone.lon) : null;
  const lastKnownLat = drone?.last_known_lat != null ? Number(drone.last_known_lat) : null;
  const lastKnownLon = drone?.last_known_lon != null ? Number(drone.last_known_lon) : null;

  const isLiveCoordValid =
    liveLat != null &&
    liveLon != null &&
    !isNaN(liveLat) &&
    !isNaN(liveLon) &&
    !(liveLat === 0 && liveLon === 0);

  const isLastKnownCoordValid =
    lastKnownLat != null &&
    lastKnownLon != null &&
    !isNaN(lastKnownLat) &&
    !isNaN(lastKnownLon) &&
    !(lastKnownLat === 0 && lastKnownLon === 0);

  // Active coordinates for drone marker: ALWAYS guarantees a valid coordinate so marker is never lost
  const droneLat = isLiveCoordValid ? liveLat : (isLastKnownCoordValid ? lastKnownLat : homeLat);
  const droneLon = isLiveCoordValid ? liveLon : (isLastKnownCoordValid ? lastKnownLon : homeLon);
  const hasDroneCoords = droneLat != null && droneLon != null && !isNaN(droneLat) && !isNaN(droneLon);
  const isDegraded = !isLiveCoordValid && isLastKnownCoordValid;
  const isAtDockFallback = !isLiveCoordValid && !isLastKnownCoordValid;

  const isAirborne =
    drone?.armed === true ||
    drone?.status === 'flying' ||
    drone?.status === 'returning' ||
    (drone?.alt_m_relative != null && Number(drone.alt_m_relative) > 1.5) ||
    (drone?.groundspeed_ms != null && Number(drone.groundspeed_ms) > 0.5);

  // Calculate live distance from drone to dock
  const distanceToDockMeters = useMemo(() => {
    if (!hasDroneCoords) return null;
    const from = L.latLng(droneLat!, droneLon!);
    const to = L.latLng(homeLat, homeLon);
    return from.distanceTo(to);
  }, [hasDroneCoords, droneLat, droneLon, homeLat, homeLon]);

  // Calculate live distance to mission target
  const distanceToTargetMeters = useMemo(() => {
    if (
      !hasDroneCoords ||
      !activeMission ||
      activeMission.target_lat == null ||
      activeMission.target_lon == null
    ) {
      return null;
    }
    const from = L.latLng(droneLat!, droneLon!);
    const to = L.latLng(Number(activeMission.target_lat), Number(activeMission.target_lon));
    return from.distanceTo(to);
  }, [hasDroneCoords, droneLat, droneLon, activeMission]);

  // Check if drone marker is inside currently visible viewport
  const checkDroneInView = useCallback(() => {
    const map = mapInstance || mapInstanceRef.current;
    if (!map || !hasDroneCoords) {
      setIsDroneInView(true);
      return;
    }
    const bounds = map.getBounds();
    const inView = bounds.contains(L.latLng(droneLat!, droneLon!));
    setIsDroneInView(inView);
  }, [mapInstance, hasDroneCoords, droneLat, droneLon]);

  // 1. Initialize Map & Container Resize Handling
  useEffect(() => {
    if (!mapContainerRef.current || mapInstanceRef.current) return;

    const initialCenter: [number, number] = [droneLat!, droneLon!];

    const map = L.map(mapContainerRef.current, {
      center: initialCenter,
      zoom: 16,
      zoomControl: false,
    });

    // OpenStreetMap tiles (styled dark via CSS filter)
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

    // Viewport move listener to update off-screen tracker
    map.on('move', () => {
      if (hasDroneCoords) {
        const inView = map.getBounds().contains(L.latLng(droneLat!, droneLon!));
        setIsDroneInView(inView);
      }
    });

    map.on('zoomend', () => {
      if (hasDroneCoords) {
        const inView = map.getBounds().contains(L.latLng(droneLat!, droneLon!));
        setIsDroneInView(inView);
      }
    });

    // Map click in picking mode updates target coordinates
    map.on('click', (e: L.LeafletMouseEvent) => {
      if (isPickingRef.current) {
        const lat = Number(e.latlng.lat.toFixed(6));
        const lon = Number(e.latlng.lng.toFixed(6));
        setPickedLat(lat);
        setPickedLon(lon);
        if (pickingMarkerRef.current) {
          pickingMarkerRef.current.setLatLng([lat, lon]);
        }
      }
    });

    mapInstanceRef.current = map;
    setMapInstance(map);

    // Invalidate map size after container mounts
    const timer = setTimeout(() => {
      map.invalidateSize();
      if (hasDroneCoords && !initialCenteredRef.current) {
        map.setView([droneLat!, droneLon!], 16);
        initialCenteredRef.current = true;
      }
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
      if (pickingMarkerRef.current) {
        try { pickingMarkerRef.current.remove(); } catch (_) {}
        pickingMarkerRef.current = null;
      }
      if (droneMarkerRef.current) {
        try { droneMarkerRef.current.remove(); } catch (_) {}
        droneMarkerRef.current = null;
      }
      if (dockMarkerRef.current) {
        try { dockMarkerRef.current.remove(); } catch (_) {}
        dockMarkerRef.current = null;
      }
      if (userMarkerRef.current) {
        try { userMarkerRef.current.remove(); } catch (_) {}
        userMarkerRef.current = null;
      }
      if (targetMarkerRef.current) {
        try { targetMarkerRef.current.remove(); } catch (_) {}
        targetMarkerRef.current = null;
      }
      if (targetLineRef.current) {
        try { targetLineRef.current.remove(); } catch (_) {}
        targetLineRef.current = null;
      }
      if (flightTrailRef.current) {
        try { flightTrailRef.current.remove(); } catch (_) {}
        flightTrailRef.current = null;
      }
      if (geofenceCircleRef.current) {
        try { geofenceCircleRef.current.remove(); } catch (_) {}
        geofenceCircleRef.current = null;
      }
      map.remove();
      mapInstanceRef.current = null;
      setMapInstance(null);
    };
  }, [homeLat, homeLon]);

  // 2. Update Dock Marker
  useEffect(() => {
    const map = mapInstance || mapInstanceRef.current;
    if (!map) return;

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

    if (!dockMarkerRef.current || !map.hasLayer(dockMarkerRef.current)) {
      if (dockMarkerRef.current) {
        try { dockMarkerRef.current.remove(); } catch (_) {}
      }
      dockMarkerRef.current = L.marker([homeLat, homeLon], {
        icon: dockIcon,
        zIndexOffset: 100,
      })
        .addTo(map)
        .bindPopup(
          `<b>Home Dock (Patna)</b><br>Lat: ${homeLat.toFixed(6)}<br>Lon: ${homeLon.toFixed(6)}`
        );
    } else {
      dockMarkerRef.current.setIcon(dockIcon);
      dockMarkerRef.current.setLatLng([homeLat, homeLon]);
    }
  }, [mapInstance, homeLat, homeLon]);

  // 3. Update User Geolocation Marker
  useEffect(() => {
    const map = mapInstance || mapInstanceRef.current;
    if (!map || !userCoords) return;

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

    if (!userMarkerRef.current || !map.hasLayer(userMarkerRef.current)) {
      if (userMarkerRef.current) {
        try { userMarkerRef.current.remove(); } catch (_) {}
      }
      userMarkerRef.current = L.marker([userCoords.lat, userCoords.lon], {
        icon: userIcon,
        zIndexOffset: 200,
      })
        .addTo(map)
        .bindPopup('<b>Operator Current Location</b>');
    } else {
      userMarkerRef.current.setIcon(userIcon);
      userMarkerRef.current.setLatLng([userCoords.lat, userCoords.lon]);
    }
  }, [mapInstance, userCoords]);

  // 4. Update Drone Marker with Clean GPS Navigation Arrow (Image 2)
  useEffect(() => {
    const map = mapInstance || mapInstanceRef.current;
    if (!map) return;

    // Auto-lock camera on aircraft when drone takes off or arms
    if (!prevAirborneRef.current && isAirborne) {
      setAutoFollow(true);
      if (hasDroneCoords) {
        map.setView([droneLat!, droneLon!], 16, { animate: true });
      }
    }
    prevAirborneRef.current = isAirborne;

    // Initial center on drone position when loaded
    if (hasDroneCoords && !initialCenteredRef.current) {
      map.setView([droneLat!, droneLon!], 16, { animate: true });
      initialCenteredRef.current = true;
    }

    if (hasDroneCoords) {
      const currentPos: [number, number] = [droneLat!, droneLon!];
      const rawHeading = Math.round(Number(drone?.heading_deg ?? 0));
      const compassDir = getCompassDirection(rawHeading);
      const altM = drone?.alt_m_relative != null ? drone.alt_m_relative.toFixed(1) : '0';
      const spdMs = drone?.groundspeed_ms != null ? drone.groundspeed_ms.toFixed(1) : '0';

      // Shortest angular turn interpolation to prevent 360-degree spin loops across North (0 deg)
      let headingDelta = (rawHeading - ((cumulativeHeadingRef.current % 360) + 360) % 360);
      if (headingDelta > 180) headingDelta -= 360;
      if (headingDelta < -180) headingDelta += 360;
      cumulativeHeadingRef.current += headingDelta;
      const smoothedHeading = cumulativeHeadingRef.current;

      const arrowColor = isDegraded
        ? '#e0a51b'
        : isAirborne
        ? '#00e5ff'
        : isAtDockFallback
        ? '#00e5ff'
        : '#00e5ff';

      const iconStateKey = `${isAirborne ? 'airborne' : 'docked'}-${isDegraded ? 'degraded' : 'normal'}`;

      // CLEAN CRISP GPS NAVIGATION ARROW (Matching user's Image 2 reference with dynamic heading orientation & forward vision beam)
      const droneIcon = L.divIcon({
        className: 'custom-drone-tactical-marker',
        html: `
          <div style="position:relative; width:56px; height:56px; display:flex; align-items:center; justify-content:center; pointer-events:auto;">
            <!-- Radar Beacon Pulse Ring (when airborne) -->
            ${
              isAirborne
                ? `<div style="position:absolute; width:44px; height:44px; border-radius:50%; background:rgba(0,229,255,0.22); border:1.5px solid #00e5ff; animation:ping 1.6s cubic-bezier(0,0,0.2,1) infinite;"></div>`
                : ''
            }

            <!-- Dynamic Heading Rotator Wrapper (Smooth 60fps Rotation) -->
            <div
              class="drone-heading-rotator"
              style="position:absolute; inset:0; display:flex; align-items:center; justify-content:center; transform:rotate(${smoothedHeading}deg); transform-origin:50% 50%; transition:transform 0.3s cubic-bezier(0.25, 1, 0.5, 1); will-change:transform;"
            >
              <svg width="56" height="56" viewBox="0 0 56 56" style="display:block; overflow:visible;">
                <defs>
                  <!-- Forward Vision / Look-Ahead Radar Beam Gradient (Shows exactly where drone is looking) -->
                  <linearGradient id="droneLookBeam" x1="0%" y1="100%" x2="0%" y2="0%">
                    <stop offset="0%" stop-color="#00e5ff" stop-opacity="0.38" />
                    <stop offset="60%" stop-color="#00e5ff" stop-opacity="0.12" />
                    <stop offset="100%" stop-color="#00e5ff" stop-opacity="0.0" />
                  </linearGradient>
                  <!-- Crisp Drop Shadow -->
                  <filter id="droneArrowShadow" x="-20%" y="-20%" width="140%" height="140%">
                    <feDropShadow dx="0" dy="2" stdDeviation="3.5" flood-color="#000000" flood-opacity="0.95" />
                  </filter>
                </defs>

                <!-- 1. Forward Field of View / Look-Ahead Cone (Where Drone Nose is Facing) -->
                <polygon
                  points="28,28 17,2 39,2"
                  fill="url(#droneLookBeam)"
                />
                <line x1="28" y1="28" x2="28" y2="1" stroke="#00e5ff" stroke-width="1" stroke-dasharray="2,2" opacity="0.8" />

                <!-- 2. Tactical Navigation Delta Arrow (Image 2) -->
                <g filter="url(#droneArrowShadow)">
                  <!-- Arrow Body: Center is (28, 28). Nose: (28, 10). Wings: (41, 44), (15, 44). Notch: (28, 38) -->
                  <path
                    d="M 28 10 L 41 44 L 28 38 L 15 44 Z"
                    fill="${arrowColor}"
                    stroke="#050b14"
                    stroke-width="2.5"
                    stroke-linejoin="round"
                    stroke-linecap="round"
                  />

                  <!-- Forward Center Spine (Pointing directly to nose) -->
                  <line
                    x1="28"
                    y1="38"
                    x2="28"
                    y2="12"
                    stroke="#ffffff"
                    stroke-width="1.8"
                    stroke-linecap="round"
                    opacity="0.95"
                  />

                  <!-- Forward Nose Tip Head Indicator Dot -->
                  <circle
                    cx="28"
                    cy="11.5"
                    r="2.2"
                    fill="#ffffff"
                    stroke="#050b14"
                    stroke-width="1"
                  />
                </g>
              </svg>
            </div>
          </div>
        `,
        iconSize: [56, 56],
        iconAnchor: [28, 28],
      });

      if (!droneMarkerRef.current || !map.hasLayer(droneMarkerRef.current)) {
        if (droneMarkerRef.current) {
          try { droneMarkerRef.current.remove(); } catch (_) {}
        }
        droneMarkerRef.current = L.marker(currentPos, {
          icon: droneIcon,
          zIndexOffset: 10000,
        }).addTo(map);
        lastIconStateRef.current = iconStateKey;
      } else {
        droneMarkerRef.current.setLatLng(currentPos);
        if (lastIconStateRef.current !== iconStateKey) {
          droneMarkerRef.current.setIcon(droneIcon);
          lastIconStateRef.current = iconStateKey;
        }
        // Direct DOM transform update for smooth 60fps continuous rotation
        const markerEl = droneMarkerRef.current.getElement();
        if (markerEl) {
          const rotator = markerEl.querySelector('.drone-heading-rotator') as HTMLElement | null;
          if (rotator) {
            rotator.style.transform = `rotate(${smoothedHeading}deg)`;
          }
        }
      }

      droneMarkerRef.current.bindPopup(
        `<div style="font-family: monospace; font-size: 11px; line-height: 1.5;">
          <b style="color: ${arrowColor}; font-size: 12px;">${drone?.name || 'Hexacopter-1'}</b> (${isAirborne ? 'AIRBORNE' : 'DOCKED'})<br/>
          <span>Status: <b style="color:#ffffff;">${drone?.status?.toUpperCase() || 'DOCKED'}</b></span><br/>
          <span>Fix: <b>${isLiveCoordValid ? (drone?.position_valid ? '3D RTK Fix' : 'GPS 3D Fix') : isDegraded ? 'Last Known Fix' : 'Dock Position'}</b></span><br/>
          <span>Lat: <b>${droneLat!.toFixed(6)}</b></span><br/>
          <span>Lon: <b>${droneLon!.toFixed(6)}</b></span><br/>
          <span>Heading: <b style="color:${arrowColor};">${rawHeading}° (${compassDir})</b></span><br/>
          <span>Alt: <b>${altM} m rel</b> (${drone?.alt_m_amsl?.toFixed(1) || '0'} m AMSL)</span><br/>
          <span>Speed: <b>${spdMs} m/s</b> (${((drone?.groundspeed_ms ?? 0) * 3.6).toFixed(1)} km/h)</span><br/>
          <span>Mode: <b>${drone?.mode || '—'}</b> (${drone?.control_mode || 'AUTO'})</span>
        </div>`
      );

      // Append to Flight Breadcrumb Trail
      if (showTrail) {
        const history = trailPointsRef.current;
        const last = history[history.length - 1];
        if (!last || map.distance(last, currentPos) >= 0.5) {
          history.push(currentPos);
          if (history.length > 800) {
            history.shift();
          }
          setTrailCount(history.length);
        }

        if (!flightTrailRef.current || !map.hasLayer(flightTrailRef.current)) {
          if (flightTrailRef.current) {
            try { flightTrailRef.current.remove(); } catch (_) {}
          }
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

      // Auto-Follow Camera Lock
      if (autoFollow) {
        map.panTo(currentPos, { animate: true, duration: 0.35 });
      }

      // Update in-view status
      checkDroneInView();
    }
  }, [mapInstance, hasDroneCoords, droneLat, droneLon, drone, isAirborne, isDegraded, isAtDockFallback, isLiveCoordValid, autoFollow, showTrail, checkDroneInView]);

  // 5. Update Active Mission Target & Waypoint Vector Line
  useEffect(() => {
    const map = mapInstance || mapInstanceRef.current;
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

      if (!targetMarkerRef.current || !map.hasLayer(targetMarkerRef.current)) {
        if (targetMarkerRef.current) {
          try { targetMarkerRef.current.remove(); } catch (_) {}
        }
        targetMarkerRef.current = L.marker(targetPos, { icon: targetIcon })
          .addTo(map)
          .bindPopup(`<b>Mission Target Waypoint</b><br>Type: ${activeMission?.type?.toUpperCase()}<br>Lat: ${tLat.toFixed(6)}<br>Lon: ${tLon.toFixed(6)}`);
      } else {
        targetMarkerRef.current.setIcon(targetIcon);
        targetMarkerRef.current.setLatLng(targetPos);
      }

      // Connecting corridor line from current drone position (or dock) to target
      const startPos: [number, number] = [droneLat!, droneLon!];

      if (!targetLineRef.current || !map.hasLayer(targetLineRef.current)) {
        if (targetLineRef.current) {
          try { targetLineRef.current.remove(); } catch (_) {}
        }
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
        try { targetMarkerRef.current.remove(); } catch (_) {}
        targetMarkerRef.current = null;
      }
      if (targetLineRef.current) {
        try { targetLineRef.current.remove(); } catch (_) {}
        targetLineRef.current = null;
      }
    }
  }, [mapInstance, activeMission, hasDroneCoords, droneLat, droneLon, homeLat, homeLon]);

  // Center / Framing Handlers
  const handleCenterOnDrone = () => {
    const map = mapInstance || mapInstanceRef.current;
    if (!map) return;
    setAutoFollow(true);
    if (hasDroneCoords) {
      map.setView([droneLat!, droneLon!], 16, { animate: true });
      setIsDroneInView(true);
    } else {
      map.setView([homeLat, homeLon], 15, { animate: true });
    }
  };

  const handleCenterOnUser = () => {
    const map = mapInstance || mapInstanceRef.current;
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
    const map = mapInstance || mapInstanceRef.current;
    if (!map) return;
    setAutoFollow(false);

    const points: [number, number][] = [[homeLat, homeLon]];
    if (hasDroneCoords) {
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
    const map = mapInstance || mapInstanceRef.current;
    trailPointsRef.current = [];
    setTrailCount(0);
    if (flightTrailRef.current && map) {
      try { flightTrailRef.current.remove(); } catch (_) {}
      flightTrailRef.current = null;
    }
  };

  const formatDistanceLabel = (meters: number | null) => {
    if (meters == null || isNaN(meters)) return '—';
    if (meters < 1000) return `${Math.round(meters)} m`;
    return `${(meters / 1000).toFixed(2)} km`;
  };

  // Target coordinates validation & boundary calculation
  const latNum = typeof pickedLat === 'number' ? pickedLat : parseFloat(String(pickedLat));
  const lonNum = typeof pickedLon === 'number' ? pickedLon : parseFloat(String(pickedLon));
  const isValidCoords =
    pickedLat !== '' &&
    pickedLon !== '' &&
    !isNaN(latNum) &&
    !isNaN(lonNum) &&
    latNum >= -90 &&
    latNum <= 90 &&
    lonNum >= -180 &&
    lonNum <= 180;

  const distanceFromDockKm = isValidCoords
    ? computeHaversineKm(homeLat, homeLon, latNum, lonNum)
    : 0;
  const distanceFromDockMeters = distanceFromDockKm * 1000;
  const isInsideGeofence = isValidCoords && distanceFromDockMeters <= APP_CONFIG.MAX_RADIUS_M;

  // Set Target Handlers
  const handleStartPicking = () => {
    const map = mapInstance || mapInstanceRef.current;
    if (!map) return;

    const initLat = Number(homeLat.toFixed(6));
    const initLon = Number(homeLon.toFixed(6));

    setPickedLat(initLat);
    setPickedLon(initLon);
    setIsPickingMode(true);
    isPickingRef.current = true;
    setPickingError(null);
    setAutoFollow(false);

    if (pickingMarkerRef.current) {
      pickingMarkerRef.current.setLatLng([initLat, initLon]);
    } else {
      const pickingIcon = L.divIcon({
        className: 'custom-picking-target-icon',
        html: `
          <div style="position:relative; width:44px; height:44px; display:flex; align-items:center; justify-content:center; cursor:grab;">
            <div style="position:absolute; inset:0; border-radius:50%; background:rgba(0,229,255,0.25); border:2px dashed #00e5ff; animation:spin 8s linear infinite;"></div>
            <div style="position:absolute; width:16px; height:16px; border-radius:50%; background:#00e5ff; box-shadow:0 0 14px #00e5ff;"></div>
            <div style="position:absolute; width:2px; height:36px; background:#00e5ff;"></div>
            <div style="position:absolute; width:36px; height:2px; background:#00e5ff;"></div>
          </div>
        `,
        iconSize: [44, 44],
        iconAnchor: [22, 22],
      });

      const marker = L.marker([initLat, initLon], {
        icon: pickingIcon,
        draggable: true,
        zIndexOffset: 3000,
      }).addTo(map);

      marker.on('drag', (e: L.LeafletEvent) => {
        const latlng = (e.target as L.Marker).getLatLng();
        setPickedLat(Number(latlng.lat.toFixed(6)));
        setPickedLon(Number(latlng.lng.toFixed(6)));
      });

      pickingMarkerRef.current = marker;
    }

    map.setView([initLat, initLon], 15, { animate: true });
  };

  const handleCancelPicking = () => {
    setIsPickingMode(false);
    isPickingRef.current = false;
    setPickingError(null);
    setPickedLat('');
    setPickedLon('');

    const map = mapInstance || mapInstanceRef.current;
    if (pickingMarkerRef.current && map) {
      try { pickingMarkerRef.current.remove(); } catch (_) {}
      pickingMarkerRef.current = null;
    }
  };

  const handleTogglePickingMode = () => {
    if (isPickingMode) {
      handleCancelPicking();
    } else {
      handleStartPicking();
    }
  };

  const handleLatChange = (val: string) => {
    if (val === '') {
      setPickedLat('');
      return;
    }
    const num = parseFloat(val);
    setPickedLat(isNaN(num) ? '' : num);
    const numLon = typeof pickedLon === 'number' ? pickedLon : parseFloat(String(pickedLon));
    if (!isNaN(num) && !isNaN(numLon) && num >= -90 && num <= 90 && numLon >= -180 && numLon <= 180) {
      pickingMarkerRef.current?.setLatLng([num, numLon]);
    }
  };

  const handleLonChange = (val: string) => {
    if (val === '') {
      setPickedLon('');
      return;
    }
    const num = parseFloat(val);
    setPickedLon(isNaN(num) ? '' : num);
    const numLat = typeof pickedLat === 'number' ? pickedLat : parseFloat(String(pickedLat));
    if (!isNaN(numLat) && !isNaN(num) && numLat >= -90 && numLat <= 90 && num >= -180 && num <= 180) {
      pickingMarkerRef.current?.setLatLng([numLat, num]);
    }
  };

  const handleConfirmSendGoto = async () => {
    if (!isValidCoords || !isInsideGeofence) return;

    setIsSendingLocal(true);
    setPickingError(null);

    try {
      if (onIssueCommand) {
        const res = await onIssueCommand('goto', {
          target_lat: latNum,
          target_lon: lonNum,
        });

        if (res.success) {
          setPickingSuccess(`✓ Goto command dispatched to ${latNum.toFixed(4)}, ${lonNum.toFixed(4)}!`);
          handleCancelPicking();
          setTimeout(() => setPickingSuccess(null), 4000);
        } else {
          setPickingError(res.error || 'Failed to dispatch goto command.');
        }
      }
    } catch (err: any) {
      setPickingError(err?.message || 'Error issuing goto command');
    } finally {
      setIsSendingLocal(false);
    }
  };

  const headingVal = Math.round(Number(drone?.heading_deg ?? 0));
  const compassStr = getCompassDirection(headingVal);

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
              className="bg-tactical-warn/20 hover:bg-tactical-warn/30 border border-tactical-warn text-tactical-warn px-3 py-1.5 rounded-xl shadow-xl flex items-center gap-2 text-xs font-mono font-bold transition-all animate-pulse cursor-pointer"
              title="Click to resume camera lock on drone"
            >
              <Locate className="w-3.5 h-3.5 shrink-0" />
              <span>FREE CAMERA (CLICK TO LOCK)</span>
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

      {/* Top Center: Off-Screen Airborne Drone Floating Tracker */}
      {!isDroneInView && hasDroneCoords && (
        <button
          onClick={handleCenterOnDrone}
          className="absolute top-4 left-1/2 -translate-x-1/2 z-30 bg-bg-card/95 hover:bg-bg-cardElevated backdrop-blur-md border-2 border-tactical-cyan px-4 py-2 rounded-2xl shadow-[0_0_25px_rgba(0,229,255,0.4)] flex items-center gap-2.5 text-xs font-mono font-bold text-white transition-all active:scale-95 animate-pulse cursor-pointer"
          title="Click to immediately center camera on airborne drone"
        >
          <Plane className="w-4 h-4 text-tactical-cyan shrink-0 animate-bounce" />
          <span className="text-tactical-cyan font-black">
            {isAirborne ? 'AIRBORNE DRONE' : 'DRONE OFF-SCREEN'}:
          </span>
          <span>
            {headingVal}° {compassStr} • {formatNumber(drone?.alt_m_relative, 1, 'm')} Alt
          </span>
          {distanceToDockMeters != null && (
            <span className="text-gray-400 hidden sm:inline">
              ({formatDistanceLabel(distanceToDockMeters)} away)
            </span>
          )}
          <span className="px-2 py-0.5 rounded-md bg-tactical-cyan text-black font-extrabold text-[11px] ml-1">
            TRACK
          </span>
        </button>
      )}

      {/* Top Right: Live Telemetry & Radar HUD OR Set Target Panel */}
      <div className="absolute top-4 right-4 z-20 w-[calc(100%-2rem)] sm:w-[320px] max-w-[340px]">
        {isPickingMode ? (
          /* 1. SET TARGET LOCATION PICKER & PREVIEW PANEL (Replaces Flight Radar when active) */
          <div className="bg-bg-card/95 backdrop-blur-md border-2 border-tactical-cyan/40 rounded-2xl p-3.5 sm:p-4 shadow-2xl space-y-3 animate-fade-in text-xs font-mono">
            <div className="flex items-center justify-between pb-2 border-b border-bg-line">
              <div className="flex items-center gap-1.5 text-white font-bold text-xs uppercase tracking-wider">
                <Target className="w-4 h-4 text-tactical-cyan animate-pulse" />
                <span>SET TARGET LOCATION</span>
              </div>
              <button
                onClick={handleCancelPicking}
                className="text-gray-400 hover:text-white p-1 rounded-lg hover:bg-bg-line/50 transition-colors cursor-pointer"
                title="Cancel picking target"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* Coordinate Inputs */}
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="block text-[10px] text-gray-400 uppercase font-sans mb-1">
                  Latitude
                </label>
                <input
                  type="number"
                  step="0.000001"
                  value={pickedLat}
                  onChange={(e) => handleLatChange(e.target.value)}
                  placeholder="25.593200"
                  className="w-full bg-bg-input border border-bg-line focus:border-tactical-cyan text-white text-xs font-mono px-2.5 py-1.5 rounded-xl outline-none"
                />
              </div>
              <div>
                <label className="block text-[10px] text-gray-400 uppercase font-sans mb-1">
                  Longitude
                </label>
                <input
                  type="number"
                  step="0.000001"
                  value={pickedLon}
                  onChange={(e) => handleLonChange(e.target.value)}
                  placeholder="85.204500"
                  className="w-full bg-bg-input border border-bg-line focus:border-tactical-cyan text-white text-xs font-mono px-2.5 py-1.5 rounded-xl outline-none"
                />
              </div>
            </div>

            <p className="text-[10px] text-gray-400 font-sans">
              * Drag the cyan marker on map or edit coordinates.
            </p>

            {/* Error Message if insert failed */}
            {pickingError && (
              <div className="p-2 rounded-xl bg-tactical-bad/15 border border-tactical-bad/40 text-tactical-bad text-[11px] font-mono flex items-center gap-1.5">
                <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
                <span>{pickingError}</span>
              </div>
            )}

            {/* Boundary Check Warning or Preview Card */}
            {isValidCoords ? (
              !isInsideGeofence ? (
                <div className="p-2.5 rounded-xl bg-tactical-bad/15 border border-tactical-bad/40 text-tactical-bad space-y-1">
                  <div className="flex items-center gap-1.5 text-xs font-bold">
                    <AlertTriangle className="w-3.5 h-3.5 shrink-0 animate-bounce" />
                    <span>⚠️ Target is outside the boundary</span>
                  </div>
                  <p className="text-[10px] text-gray-300 font-sans">
                    Target is {distanceFromDockKm.toFixed(2)} km from dock. Geofence limit: {(APP_CONFIG.MAX_RADIUS_M / 1000).toFixed(1)} km.
                  </p>
                </div>
              ) : (
                /* Preview Card (Inside boundary) */
                <div className="bg-bg-input border border-bg-line rounded-xl p-2.5 space-y-1.5 text-[11px]">
                  <div className="flex items-center justify-between font-mono">
                    <span className="text-gray-400">Target Coords:</span>
                    <span className="text-tactical-cyan font-bold truncate max-w-[170px]">
                      {latNum.toFixed(6)}, {lonNum.toFixed(6)}
                    </span>
                  </div>
                  <div className="flex items-center justify-between font-mono">
                    <span className="text-gray-400">Distance from Dock:</span>
                    <span className="text-white font-bold">{distanceFromDockKm.toFixed(2)} km</span>
                  </div>
                </div>
              )
            ) : (
              <div className="text-[11px] text-tactical-warn font-mono p-1.5">
                Enter valid numerical coordinates.
              </div>
            )}

            {/* Action Buttons */}
            <div className="flex items-center justify-end gap-2 pt-1">
              <button
                onClick={handleCancelPicking}
                className="px-3.5 py-1.5 rounded-xl bg-bg-cardElevated hover:bg-bg-line text-xs font-semibold text-gray-300 transition-colors cursor-pointer"
              >
                Cancel
              </button>
              <button
                onClick={handleConfirmSendGoto}
                disabled={isSendingLocal || isSending || !isValidCoords || !isInsideGeofence}
                className="px-4 py-2 rounded-xl bg-tactical-cyan hover:bg-tactical-cyan/90 text-black text-xs font-bold transition-all disabled:opacity-40 disabled:cursor-not-allowed shadow-lg shadow-tactical-cyan/20 flex items-center gap-1.5 cursor-pointer"
              >
                <Send className="w-3.5 h-3.5" />
                <span>{isSendingLocal || isSending ? 'Sending...' : 'Send Drone Here'}</span>
              </button>
            </div>
          </div>
        ) : (
          /* 2. LIVE FLIGHT RADAR & TELEMETRY HUD (Visible when not picking target) */
          <div className="hidden sm:flex flex-col gap-2.5 bg-bg-card/95 backdrop-blur border border-bg-line p-3.5 rounded-2xl shadow-2xl text-xs font-mono w-full">
            <div className="flex items-center justify-between pb-2 border-b border-bg-line">
              <div className="flex items-center gap-1.5 text-tactical-cyan font-bold">
                <Radio className="w-4 h-4 animate-pulse" />
                <span>LIVE FLIGHT RADAR</span>
              </div>
              <span className="text-[10px] text-gray-400 font-bold">
                {isAirborne ? (
                  <span className="text-tactical-cyan animate-pulse">● AIRBORNE</span>
                ) : (
                  drone?.status?.toUpperCase() || 'DOCKED'
                )}
              </span>
            </div>

            {/* Live GPS Coordinates */}
            <div className="space-y-1">
              <div className="text-[10px] text-gray-400 uppercase tracking-wider flex items-center justify-between">
                <span>GPS Position</span>
                <span className={isLiveCoordValid && drone?.position_valid ? 'text-tactical-ok font-bold' : 'text-tactical-warn'}>
                  {isLiveCoordValid && drone?.position_valid ? '3D FIX' : isDegraded ? 'LAST KNOWN' : 'DOCK REF'}
                </span>
              </div>
              <div className="text-xs font-bold text-tactical-cyan font-mono truncate">
                {hasDroneCoords
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
                  {headingVal}° {compassStr}
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
                className={`flex items-center gap-1 transition-colors cursor-pointer ${
                  showTrail ? 'text-tactical-cyan' : 'text-gray-500'
                }`}
              >
                <Route className="w-3.5 h-3.5" />
                <span>Trail {showTrail ? 'ON' : 'OFF'}</span>
              </button>

              {trailCount > 0 && (
                <button
                  onClick={handleClearTrail}
                  className="text-gray-400 hover:text-tactical-bad flex items-center gap-1 transition-colors cursor-pointer"
                  title="Clear flight trail"
                >
                  <Trash2 className="w-3 h-3" />
                  <span>Clear</span>
                </button>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Success Toast */}
      {pickingSuccess && (
        <div className="absolute top-16 left-1/2 -translate-x-1/2 z-30 bg-tactical-ok/90 backdrop-blur text-black px-4 py-2.5 rounded-xl font-bold text-xs shadow-2xl flex items-center gap-2 border border-tactical-ok animate-fade-in">
          <CheckCircle2 className="w-4 h-4 shrink-0" />
          <span>{pickingSuccess}</span>
        </div>
      )}

      {/* Bottom Left: Tactical Interactive Map Controls */}
      <div className="absolute bottom-6 left-4 z-10 flex flex-col gap-2">
        {/* Set Target Button */}
        <button
          onClick={handleTogglePickingMode}
          className={`p-3 rounded-xl border shadow-xl transition-all active:scale-95 flex items-center gap-2 text-xs font-bold cursor-pointer ${
            isPickingMode
              ? 'bg-tactical-cyan text-black border-tactical-cyan shadow-tactical-cyan/20 ring-2 ring-tactical-cyan/50'
              : 'bg-bg-card/90 hover:bg-bg-card border-bg-line text-tactical-cyan'
          }`}
          title="Pick target coordinates on map"
        >
          <Target className="w-4 h-4" />
          <span className="hidden sm:inline">
            {isPickingMode ? 'Picking Target...' : 'Set Target'}
          </span>
        </button>

        {/* Center / Follow Drone */}
        <button
          onClick={handleCenterOnDrone}
          className={`p-3 rounded-xl border shadow-xl transition-all active:scale-95 flex items-center gap-2 text-xs font-bold cursor-pointer ${
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
          className="p-3 rounded-xl bg-bg-card/90 hover:bg-bg-card border border-bg-line text-gray-200 hover:text-white shadow-xl transition-all active:scale-95 flex items-center gap-2 text-xs font-bold cursor-pointer"
          title="Fit full operational corridor (Drone, Dock, Target, Operator)"
        >
          <Maximize2 className="w-4 h-4 text-tactical-cyan" />
          <span className="hidden sm:inline">Fit All View</span>
        </button>

        {/* My Geolocation */}
        <button
          onClick={handleCenterOnUser}
          className="p-3 rounded-xl bg-bg-card/90 hover:bg-bg-card border border-bg-line text-tactical-blue shadow-xl transition-all active:scale-95 flex items-center gap-2 text-xs font-bold cursor-pointer"
          title="Center map on Operator Location"
        >
          <Crosshair className="w-4 h-4" />
          <span className="hidden sm:inline">My Location</span>
        </button>
      </div>
    </div>
  );
};
