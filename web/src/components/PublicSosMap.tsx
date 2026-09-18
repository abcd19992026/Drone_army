import React, { useEffect, useRef } from 'react';
import L from 'leaflet';
import { MapPin, ExternalLink, Crosshair } from 'lucide-react';
import type { SosRecentLocation, SosPlainStatus } from '../types';

interface PublicSosMapProps {
  locations: SosRecentLocation[];
  plainStatus: SosPlainStatus;
}

export const PublicSosMap: React.FC<PublicSosMapProps> = ({ locations }) => {
  const mapContainerRef = useRef<HTMLDivElement>(null);
  const mapInstanceRef = useRef<L.Map | null>(null);
  const layerGroupRef = useRef<L.LayerGroup | null>(null);

  const latestLocation = locations && locations.length > 0 ? locations[0] : null;

  useEffect(() => {
    if (!mapContainerRef.current) return;

    // Initialize Map if not already created
    if (!mapInstanceRef.current) {
      const initialLat = latestLocation?.lat ?? 25.5932;
      const initialLon = latestLocation?.lon ?? 85.2045;

      const map = L.map(mapContainerRef.current, {
        zoomControl: false,
        attributionControl: false,
      }).setView([initialLat, initialLon], 16);

      // Standard OSM Tile Layer with app dark styling
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19,
      }).addTo(map);

      // Layer group for dynamic markers/layers
      const layerGroup = L.layerGroup().addTo(map);
      layerGroupRef.current = layerGroup;

      mapInstanceRef.current = map;

      // Invalidate size to ensure container renders correctly
      setTimeout(() => {
        map.invalidateSize();
      }, 150);
    }

    return () => {
      if (mapInstanceRef.current) {
        mapInstanceRef.current.remove();
        mapInstanceRef.current = null;
      }
    };
  }, []);

  // Update markers and view when locations change
  useEffect(() => {
    const map = mapInstanceRef.current;
    const layerGroup = layerGroupRef.current;
    if (!map || !layerGroup) return;

    layerGroup.clearLayers();

    if (!latestLocation || latestLocation.lat == null || latestLocation.lon == null) {
      return;
    }

    const { lat, lon, accuracy_m } = latestLocation;

    // 1. Accuracy Circle
    if (accuracy_m && accuracy_m > 0) {
      L.circle([lat, lon], {
        radius: Math.min(accuracy_m, 200),
        color: '#e0533d',
        fillColor: '#e0533d',
        fillOpacity: 0.12,
        weight: 1.5,
        dashArray: '4, 4',
      }).addTo(layerGroup);
    }

    // 2. Trail Polyline & Historical Waypoints (if multiple points)
    if (locations.length > 1) {
      const latLngs = locations
        .filter((l) => l.lat != null && l.lon != null)
        .map((l) => [l.lat, l.lon] as [number, number]);

      if (latLngs.length > 1) {
        L.polyline(latLngs, {
          color: '#e0533d',
          weight: 2.5,
          opacity: 0.6,
          dashArray: '6, 6',
        }).addTo(layerGroup);

        // Historical markers (older points)
        for (let i = 1; i < locations.length; i++) {
          const hist = locations[i];
          if (hist.lat != null && hist.lon != null) {
            const histIcon = L.divIcon({
              className: 'custom-hist-dot',
              html: `<div style="width: 10px; height: 10px; border-radius: 50%; background: #e0a51b; border: 1.5px solid #0f1216; opacity: 0.7;"></div>`,
              iconSize: [10, 10],
              iconAnchor: [5, 5],
            });
            L.marker([hist.lat, hist.lon], { icon: histIcon }).addTo(layerGroup);
          }
        }
      }
    }

    // 3. Main SOS Pulsing Marker
    const sosMarkerIcon = L.divIcon({
      className: 'custom-sos-marker',
      html: `
        <div style="position: relative; width: 44px; height: 44px; display: flex; align-items: center; justify-content: center;">
          <div style="position: absolute; width: 44px; height: 44px; border-radius: 50%; background: rgba(224, 83, 61, 0.35); animation: ping 1.5s cubic-bezier(0, 0, 0.2, 1) infinite;"></div>
          <div style="position: absolute; width: 28px; height: 28px; border-radius: 50%; background: rgba(224, 83, 61, 0.6); animation: pulse 2s infinite;"></div>
          <div style="position: relative; width: 18px; height: 18px; border-radius: 50%; background: #e0533d; border: 3px solid #ffffff; box-shadow: 0 0 16px rgba(224, 83, 61, 0.9);"></div>
        </div>
      `,
      iconSize: [44, 44],
      iconAnchor: [22, 22],
    });

    const marker = L.marker([lat, lon], { icon: sosMarkerIcon }).addTo(layerGroup);
    marker.bindPopup(`
      <div style="font-family: inherit; font-size: 12px; color: #fff; text-align: center; padding: 4px;">
        <strong style="color: #e0533d; text-transform: uppercase;">SOS Signal Location</strong><br/>
        Lat: ${lat.toFixed(6)}<br/>
        Lon: ${lon.toFixed(6)}<br/>
        ${accuracy_m ? `Accuracy: ±${Math.round(accuracy_m)}m` : ''}
      </div>
    `);

    // Pan map to latest position
    map.panTo([lat, lon], { animate: true });
  }, [locations, latestLocation]);

  const handleRecenter = () => {
    if (mapInstanceRef.current && latestLocation?.lat != null && latestLocation?.lon != null) {
      mapInstanceRef.current.setView([latestLocation.lat, latestLocation.lon], 16, { animate: true });
    }
  };

  const googleMapsUrl = latestLocation
    ? `https://www.google.com/maps/search/?api=1&query=${latestLocation.lat},${latestLocation.lon}`
    : '#';

  const appleMapsUrl = latestLocation
    ? `https://maps.apple.com/?q=${latestLocation.lat},${latestLocation.lon}`
    : '#';

  return (
    <div className="space-y-3">
      {/* Map Container */}
      <div className="relative w-full h-[280px] sm:h-[340px] rounded-2xl overflow-hidden border border-bg-line bg-bg-card shadow-xl">
        <div ref={mapContainerRef} className="w-full h-full" />

        {/* Map Overlay Badge */}
        <div className="absolute top-3 left-3 z-[400] bg-bg-card/90 backdrop-blur-md border border-bg-line rounded-lg px-2.5 py-1 text-[11px] font-mono font-bold text-tactical-bad flex items-center gap-1.5 shadow-md">
          <span className="w-2 h-2 rounded-full bg-tactical-bad animate-ping" />
          <span>INCIDENT GPS PIN</span>
        </div>

        {/* Recenter Button */}
        <button
          onClick={handleRecenter}
          title="Recenter on Incident"
          className="absolute bottom-3 right-3 z-[400] p-2 rounded-xl bg-bg-card/95 hover:bg-bg-cardElevated text-tactical-cyan border border-bg-line shadow-lg transition-all active:scale-95 cursor-pointer"
        >
          <Crosshair className="w-5 h-5" />
        </button>
      </div>

      {/* Location Details & Navigation Links */}
      {latestLocation ? (
        <div className="bg-bg-cardElevated/80 border border-bg-line rounded-xl p-3.5 space-y-2.5">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 text-xs">
            <div className="flex items-center gap-2">
              <MapPin className="w-4 h-4 text-tactical-bad shrink-0" />
              <span className="font-mono text-gray-200 font-bold">
                {latestLocation.lat.toFixed(6)}, {latestLocation.lon.toFixed(6)}
              </span>
              {latestLocation.accuracy_m != null && (
                <span className="text-[10px] text-gray-400 font-mono">
                  (±{Math.round(latestLocation.accuracy_m)}m)
                </span>
              )}
            </div>

            <div className="flex items-center gap-2">
              <a
                href={googleMapsUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="px-2.5 py-1.5 rounded-lg bg-bg-card hover:bg-bg-line text-tactical-cyan border border-tactical-cyan/30 text-[11px] font-semibold flex items-center gap-1 transition-colors"
              >
                <span>Google Maps</span>
                <ExternalLink className="w-3 h-3" />
              </a>
              <a
                href={appleMapsUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="px-2.5 py-1.5 rounded-lg bg-bg-card hover:bg-bg-line text-gray-300 border border-gray-600 text-[11px] font-semibold flex items-center gap-1 transition-colors"
              >
                <span>Apple Maps</span>
                <ExternalLink className="w-3 h-3" />
              </a>
            </div>
          </div>

          {latestLocation.recorded_at && (
            <div className="text-[10px] font-mono text-gray-500">
              GPS Timestamp: {new Date(latestLocation.recorded_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })}
            </div>
          )}
        </div>
      ) : (
        <div className="bg-bg-card border border-bg-line rounded-xl p-4 text-center text-xs text-gray-400 font-mono">
          Acquiring GPS fix for this emergency signal...
        </div>
      )}
    </div>
  );
};
