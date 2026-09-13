import React, { useState } from 'react';
import {
  CalendarClock,
  Plus,
  Pencil,
  Trash2,
  Play,
  Pause,
  Clock,
  MapPin,
  AlertTriangle,
  CheckCircle2,
  XCircle,
  X,
  RotateCcw,
  Layers,
} from 'lucide-react';
import { Badge } from '../components/Badge';
import { formatFullDateTime, timeAgo } from '../lib/utils';
import type {
  PatrolScheduleRow,
  PatrolScheduleFormData,
  ScheduleCadence,
} from '../types';

const DAYS_OF_WEEK = [
  { value: 0, label: 'Sun', full: 'Sunday' },
  { value: 1, label: 'Mon', full: 'Monday' },
  { value: 2, label: 'Tue', full: 'Tuesday' },
  { value: 3, label: 'Wed', full: 'Wednesday' },
  { value: 4, label: 'Thu', full: 'Thursday' },
  { value: 5, label: 'Fri', full: 'Friday' },
  { value: 6, label: 'Sat', full: 'Saturday' },
];

const INITIAL_FORM: PatrolScheduleFormData = {
  name: '',
  target_lat: '',
  target_lon: '',
  cruise_alt_m: '',
  loiter_seconds: 120,
  cadence: 'daily',
  time_of_day: '07:00',
  days_of_week: [1, 2, 3, 4, 5], // Default Mon-Fri
  active: true,
};

interface PatrolSchedulesScreenProps {
  schedules: PatrolScheduleRow[];
  loading: boolean;
  actionLoading: boolean;
  statusMessage: { text: string; type: 'info' | 'success' | 'error' } | null;
  onCreateSchedule: (
    data: PatrolScheduleFormData
  ) => Promise<{ success: boolean; error?: string }>;
  onUpdateSchedule: (
    id: string,
    data: PatrolScheduleFormData
  ) => Promise<{ success: boolean; error?: string }>;
  onToggleActive: (
    id: string,
    currentActive: boolean
  ) => Promise<{ success: boolean; error?: string }>;
  onDeleteSchedule: (
    id: string
  ) => Promise<{ success: boolean; error?: string }>;
}

export const PatrolSchedulesScreen: React.FC<PatrolSchedulesScreenProps> = ({
  schedules,
  loading,
  actionLoading,
  statusMessage,
  onCreateSchedule,
  onUpdateSchedule,
  onToggleActive,
  onDeleteSchedule,
}) => {
  // Modal states
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [editingScheduleId, setEditingScheduleId] = useState<string | null>(null);
  const [formData, setFormData] = useState<PatrolScheduleFormData>(INITIAL_FORM);
  const [formError, setFormError] = useState<string | null>(null);

  // Delete confirmation state
  const [deleteTarget, setDeleteTarget] = useState<PatrolScheduleRow | null>(null);

  // Filter
  const [filterCadence, setFilterCadence] = useState<'all' | 'daily' | 'weekly'>('all');

  const openAddModal = () => {
    setEditingScheduleId(null);
    setFormData(INITIAL_FORM);
    setFormError(null);
    setIsModalOpen(true);
  };

  const openEditModal = (schedule: PatrolScheduleRow) => {
    setEditingScheduleId(schedule.id);
    // time_of_day might be "07:00:00" from postgres, slice to "HH:MM"
    const timeFormatted = schedule.time_of_day ? schedule.time_of_day.slice(0, 5) : '07:00';
    setFormData({
      name: schedule.name,
      target_lat: schedule.target_lat,
      target_lon: schedule.target_lon,
      cruise_alt_m: schedule.cruise_alt_m ?? '',
      loiter_seconds: schedule.loiter_seconds,
      cadence: schedule.cadence,
      time_of_day: timeFormatted,
      days_of_week: schedule.days_of_week ? [...schedule.days_of_week] : [1, 2, 3, 4, 5],
      active: schedule.active,
    });
    setFormError(null);
    setIsModalOpen(true);
  };

  const closeModal = () => {
    setIsModalOpen(false);
    setEditingScheduleId(null);
    setFormError(null);
  };

  const handleDayToggle = (dayVal: number) => {
    setFormData((prev) => {
      const exists = prev.days_of_week.includes(dayVal);
      const updated = exists
        ? prev.days_of_week.filter((d) => d !== dayVal)
        : [...prev.days_of_week, dayVal];
      return { ...prev, days_of_week: updated };
    });
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError(null);

    // Validation
    if (!formData.name.trim()) {
      setFormError('Schedule name is required.');
      return;
    }
    if (formData.target_lat === '' || isNaN(Number(formData.target_lat))) {
      setFormError('Valid Target Latitude is required (-90 to 90).');
      return;
    }
    const lat = Number(formData.target_lat);
    if (lat < -90 || lat > 90) {
      setFormError('Latitude must be between -90 and 90 degrees.');
      return;
    }

    if (formData.target_lon === '' || isNaN(Number(formData.target_lon))) {
      setFormError('Valid Target Longitude is required (-180 to 180).');
      return;
    }
    const lon = Number(formData.target_lon);
    if (lon < -180 || lon > 180) {
      setFormError('Longitude must be between -180 and 180 degrees.');
      return;
    }

    if (formData.loiter_seconds === '' || Number(formData.loiter_seconds) <= 0) {
      setFormError('Loiter duration must be greater than 0 seconds.');
      return;
    }

    if (!formData.time_of_day || !/^\d{2}:\d{2}$/.test(formData.time_of_day)) {
      setFormError('Time of day must be in HH:MM (IST) format.');
      return;
    }

    if (formData.cadence === 'weekly' && formData.days_of_week.length === 0) {
      setFormError('Please select at least one day of the week for weekly cadence.');
      return;
    }

    if (editingScheduleId) {
      const res = await onUpdateSchedule(editingScheduleId, formData);
      if (res.success) {
        closeModal();
      } else if (res.error) {
        setFormError(res.error);
      }
    } else {
      const res = await onCreateSchedule(formData);
      if (res.success) {
        closeModal();
      } else if (res.error) {
        setFormError(res.error);
      }
    }
  };

  const handleConfirmDelete = async () => {
    if (!deleteTarget) return;
    const res = await onDeleteSchedule(deleteTarget.id);
    if (res.success) {
      setDeleteTarget(null);
    }
  };

  // Filtered schedules
  const filteredSchedules = schedules.filter((s) => {
    if (filterCadence === 'daily') return s.cadence === 'daily';
    if (filterCadence === 'weekly') return s.cadence === 'weekly';
    return true;
  });

  const activeCount = schedules.filter((s) => s.active).length;
  const dailyCount = schedules.filter((s) => s.cadence === 'daily').length;
  const weeklyCount = schedules.filter((s) => s.cadence === 'weekly').length;

  return (
    <div className="space-y-6 max-w-7xl mx-auto pb-16">
      {/* 1. TOP HEADER & METRICS */}
      <div className="bg-bg-card border border-bg-line rounded-2xl p-5 sm:p-6 shadow-xl space-y-4">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-bg-line">
          <div>
            <div className="flex items-center gap-2.5">
              <div className="p-2 rounded-xl bg-tactical-cyan/15 border border-tactical-cyan/30 text-tactical-cyan">
                <CalendarClock className="w-5 h-5" />
              </div>
              <h1 className="text-lg sm:text-xl font-bold text-white tracking-wide">
                PATROL SCHEDULES
              </h1>
              <Badge variant="cyan" size="sm">
                RECURRING
              </Badge>
            </div>
            <p className="text-xs text-gray-400 mt-1">
              Automated periodic patrol routes dispatched on IST wall-clock schedule. GSS scheduler evaluates safety and weather before each flight.
            </p>
          </div>

          <button
            onClick={openAddModal}
            className="flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl bg-tactical-cyan text-bg font-bold text-xs uppercase tracking-wider hover:bg-tactical-cyan/90 transition-all shadow-[0_0_15px_rgba(0,229,255,0.25)] shrink-0"
          >
            <Plus className="w-4 h-4 stroke-[3]" />
            <span>Add Schedule</span>
          </button>
        </div>

        {/* Status / Feedback message */}
        {statusMessage && (
          <div
            className={`p-3.5 rounded-xl border text-xs font-mono flex items-start gap-2.5 ${
              statusMessage.type === 'error'
                ? 'bg-tactical-bad/15 border-tactical-bad/40 text-tactical-bad'
                : statusMessage.type === 'success'
                ? 'bg-tactical-ok/15 border-tactical-ok/40 text-tactical-ok'
                : 'bg-tactical-blue/15 border-tactical-blue/40 text-tactical-blue'
            }`}
          >
            {statusMessage.type === 'error' ? (
              <XCircle className="w-4 h-4 shrink-0 mt-0.5" />
            ) : (
              <CheckCircle2 className="w-4 h-4 shrink-0 mt-0.5" />
            )}
            <div className="flex-1">{statusMessage.text}</div>
          </div>
        )}

        {/* Quick Stats & Filter Bar */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 pt-1">
          <div className="p-3 rounded-xl bg-bg-input border border-bg-line">
            <div className="text-[11px] text-gray-400 font-mono">TOTAL SCHEDULES</div>
            <div className="text-lg font-bold text-white font-mono mt-0.5">
              {schedules.length}
            </div>
          </div>
          <div className="p-3 rounded-xl bg-bg-input border border-bg-line">
            <div className="text-[11px] text-gray-400 font-mono">ACTIVE (RUNNING)</div>
            <div className="text-lg font-bold text-tactical-ok font-mono mt-0.5">
              {activeCount}
            </div>
          </div>
          <div className="p-3 rounded-xl bg-bg-input border border-bg-line">
            <div className="text-[11px] text-gray-400 font-mono">DAILY PATROLS</div>
            <div className="text-lg font-bold text-tactical-cyan font-mono mt-0.5">
              {dailyCount}
            </div>
          </div>
          <div className="p-3 rounded-xl bg-bg-input border border-bg-line">
            <div className="text-[11px] text-gray-400 font-mono">WEEKLY PATROLS</div>
            <div className="text-lg font-bold text-tactical-purple font-mono mt-0.5">
              {weeklyCount}
            </div>
          </div>
        </div>

        {/* Filter Pills */}
        <div className="flex items-center gap-2 pt-2">
          <span className="text-xs text-gray-400 font-mono mr-1">Filter:</span>
          {(['all', 'daily', 'weekly'] as const).map((mode) => (
            <button
              key={mode}
              onClick={() => setFilterCadence(mode)}
              className={`px-3 py-1 rounded-lg text-xs font-mono uppercase transition-all ${
                filterCadence === mode
                  ? 'bg-tactical-cyan/20 text-tactical-cyan border border-tactical-cyan/40 font-bold'
                  : 'bg-bg-input text-gray-400 hover:text-gray-200 border border-bg-line'
              }`}
            >
              {mode === 'all' ? 'All' : mode}
            </button>
          ))}
        </div>
      </div>

      {/* 2. SCHEDULES LIST */}
      <div className="space-y-3">
        {loading ? (
          <div className="bg-bg-card border border-bg-line rounded-2xl p-12 text-center text-gray-400 font-mono text-xs flex flex-col items-center gap-3">
            <div className="w-8 h-8 border-2 border-tactical-cyan border-t-transparent rounded-full animate-spin" />
            <span>Loading patrol schedules from Supabase...</span>
          </div>
        ) : filteredSchedules.length === 0 ? (
          <div className="bg-bg-card border border-bg-line rounded-2xl p-12 text-center space-y-3">
            <CalendarClock className="w-10 h-10 text-gray-600 mx-auto" />
            <div className="text-sm font-bold text-gray-300">
              No Patrol Schedules Found
            </div>
            <p className="text-xs text-gray-500 max-w-md mx-auto">
              {filterCadence !== 'all'
                ? `No ${filterCadence} schedules matching current filter.`
                : 'Create recurring patrol routines to autonomously inspect fields, perimeters, or critical waypoints at specified IST times.'}
            </p>
            <button
              onClick={openAddModal}
              className="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-tactical-cyan/15 text-tactical-cyan border border-tactical-cyan/30 text-xs font-bold uppercase hover:bg-tactical-cyan/25 transition-all mt-2"
            >
              <Plus className="w-4 h-4" />
              <span>Create First Schedule</span>
            </button>
          </div>
        ) : (
          filteredSchedules.map((schedule) => {
            const timeDisplay = schedule.time_of_day
              ? schedule.time_of_day.slice(0, 5)
              : '—';

            return (
              <div
                key={schedule.id}
                className={`bg-bg-card border rounded-2xl p-5 shadow-lg transition-all ${
                  schedule.active
                    ? 'border-bg-line hover:border-tactical-cyan/40'
                    : 'border-bg-line/60 opacity-75 hover:opacity-100'
                }`}
              >
                <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4">
                  {/* Left info */}
                  <div className="space-y-2.5 flex-1">
                    <div className="flex flex-wrap items-center gap-2.5">
                      <span className="text-base font-bold text-white tracking-wide">
                        {schedule.name}
                      </span>

                      {/* Active Status Badge */}
                      <Badge
                        variant={schedule.active ? 'ok' : 'neutral'}
                        size="sm"
                      >
                        {schedule.active ? 'ACTIVE' : 'PAUSED'}
                      </Badge>

                      {/* Cadence Badge */}
                      <Badge
                        variant={schedule.cadence === 'daily' ? 'cyan' : 'purple'}
                        size="sm"
                      >
                        {schedule.cadence.toUpperCase()}
                      </Badge>

                      {/* Time IST */}
                      <span className="px-2 py-0.5 rounded-md bg-bg-input border border-bg-line text-xs font-mono text-tactical-cyan flex items-center gap-1">
                        <Clock className="w-3 h-3" />
                        <span>{timeDisplay} IST</span>
                      </span>
                    </div>

                    {/* Weekly Days Badges */}
                    {schedule.cadence === 'weekly' && schedule.days_of_week && (
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <span className="text-[11px] text-gray-500 font-mono">Days:</span>
                        {DAYS_OF_WEEK.map((d) => {
                          const isSelected = schedule.days_of_week?.includes(d.value);
                          return (
                            <span
                              key={d.value}
                              className={`px-1.5 py-0.5 rounded text-[10px] font-mono ${
                                isSelected
                                  ? 'bg-tactical-purple/20 border border-tactical-purple/40 text-tactical-purple font-bold'
                                  : 'bg-bg-input text-gray-600 border border-bg-line/40'
                              }`}
                            >
                              {d.label}
                            </span>
                          );
                        })}
                      </div>
                    )}

                    {/* Coordinates & Flight Params */}
                    <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 text-xs font-mono text-gray-300 pt-1">
                      <div className="flex items-center gap-1.5 bg-bg-input px-2.5 py-1.5 rounded-lg border border-bg-line">
                        <MapPin className="w-3.5 h-3.5 text-tactical-cyan shrink-0" />
                        <span className="text-gray-400">Target:</span>
                        <span className="text-white font-bold">
                          {schedule.target_lat.toFixed(5)}, {schedule.target_lon.toFixed(5)}
                        </span>
                      </div>

                      <div className="flex items-center gap-1.5 bg-bg-input px-2.5 py-1.5 rounded-lg border border-bg-line">
                        <Layers className="w-3.5 h-3.5 text-tactical-blue shrink-0" />
                        <span className="text-gray-400">Cruise Alt:</span>
                        <span className="text-white font-bold">
                          {schedule.cruise_alt_m ? `${schedule.cruise_alt_m} m` : 'Default'}
                        </span>
                      </div>

                      <div className="flex items-center gap-1.5 bg-bg-input px-2.5 py-1.5 rounded-lg border border-bg-line">
                        <RotateCcw className="w-3.5 h-3.5 text-tactical-warn shrink-0" />
                        <span className="text-gray-400">Loiter:</span>
                        <span className="text-white font-bold">
                          {schedule.loiter_seconds}s
                        </span>
                      </div>
                    </div>

                    {/* Timestamps */}
                    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] font-mono text-gray-400 pt-1">
                      <div className="flex items-center gap-1">
                        <span className="text-gray-500">Last Run:</span>
                        <span className={schedule.last_run_at ? 'text-gray-200' : 'text-gray-600'}>
                          {schedule.last_run_at
                            ? `${formatFullDateTime(schedule.last_run_at)} (${timeAgo(schedule.last_run_at)})`
                            : 'Never run'}
                        </span>
                      </div>

                      {schedule.next_run_at && !schedule.next_run_at.startsWith('1970') && (
                        <div className="flex items-center gap-1">
                          <span className="text-gray-500">Next Due:</span>
                          <span className="text-tactical-cyan">
                            {formatFullDateTime(schedule.next_run_at)}
                          </span>
                        </div>
                      )}
                    </div>

                    {/* Skipped Reason Alert if present */}
                    {schedule.last_skipped_reason && (
                      <div className="p-2.5 rounded-xl bg-tactical-warn/10 border border-tactical-warn/30 text-tactical-warn text-xs font-mono flex items-start gap-2">
                        <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
                        <div>
                          <span className="font-bold uppercase">Last Skipped: </span>
                          <span>{schedule.last_skipped_reason}</span>
                        </div>
                      </div>
                    )}
                  </div>

                  {/* Right Actions */}
                  <div className="flex items-center gap-2 pt-2 lg:pt-0 border-t lg:border-t-0 border-bg-line shrink-0">
                    {/* 1-Click Active Toggle */}
                    <button
                      onClick={() => onToggleActive(schedule.id, schedule.active)}
                      disabled={actionLoading}
                      title={schedule.active ? 'Pause Schedule' : 'Activate Schedule'}
                      className={`flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-bold uppercase tracking-wider transition-all border ${
                        schedule.active
                          ? 'bg-tactical-ok/15 text-tactical-ok border-tactical-ok/30 hover:bg-tactical-ok/25'
                          : 'bg-bg-input text-gray-400 border-bg-line hover:text-white hover:bg-bg-line'
                      }`}
                    >
                      {schedule.active ? (
                        <>
                          <Pause className="w-3.5 h-3.5 fill-current" />
                          <span>Active</span>
                        </>
                      ) : (
                        <>
                          <Play className="w-3.5 h-3.5 fill-current" />
                          <span>Paused</span>
                        </>
                      )}
                    </button>

                    {/* Edit button */}
                    <button
                      onClick={() => openEditModal(schedule)}
                      title="Edit Schedule"
                      className="p-2 rounded-xl bg-bg-input border border-bg-line text-gray-300 hover:text-tactical-cyan hover:border-tactical-cyan/40 transition-colors"
                    >
                      <Pencil className="w-4 h-4" />
                    </button>

                    {/* Delete button */}
                    <button
                      onClick={() => setDeleteTarget(schedule)}
                      title="Delete Schedule"
                      className="p-2 rounded-xl bg-bg-input border border-bg-line text-gray-400 hover:text-tactical-bad hover:border-tactical-bad/40 transition-colors"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>

      {/* 3. ADD / EDIT SCHEDULE MODAL */}
      {isModalOpen && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4 overflow-y-auto animate-fade-in">
          <div className="bg-bg-card border border-bg-line rounded-2xl max-w-xl w-full p-6 shadow-2xl space-y-5 my-8">
            <div className="flex items-center justify-between pb-3 border-b border-bg-line">
              <div className="flex items-center gap-2">
                <CalendarClock className="w-5 h-5 text-tactical-cyan" />
                <h3 className="text-base font-bold text-white">
                  {editingScheduleId ? 'EDIT PATROL SCHEDULE' : 'ADD PATROL SCHEDULE'}
                </h3>
              </div>
              <button
                onClick={closeModal}
                className="p-1 rounded-lg text-gray-400 hover:text-white hover:bg-bg-line transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            {formError && (
              <div className="p-3 rounded-xl bg-tactical-bad/15 border border-tactical-bad/40 text-tactical-bad text-xs font-mono flex items-start gap-2">
                <XCircle className="w-4 h-4 shrink-0 mt-0.5" />
                <span>{formError}</span>
              </div>
            )}

            <form onSubmit={handleSubmit} className="space-y-4">
              {/* Name */}
              <div>
                <label className="block text-xs font-mono uppercase text-gray-400 mb-1">
                  Schedule Name *
                </label>
                <input
                  type="text"
                  required
                  placeholder="e.g. Morning North Perimeter Check"
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  className="w-full px-3.5 py-2.5 rounded-xl bg-bg-input border border-bg-line text-sm text-white placeholder-gray-600 focus:outline-none focus:border-tactical-cyan font-sans"
                />
              </div>

              {/* Coordinates */}
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-mono uppercase text-gray-400 mb-1">
                    Target Latitude (-90 to 90) *
                  </label>
                  <input
                    type="number"
                    step="any"
                    required
                    placeholder="e.g. 25.612345"
                    value={formData.target_lat}
                    onChange={(e) =>
                      setFormData({
                        ...formData,
                        target_lat: e.target.value === '' ? '' : Number(e.target.value),
                      })
                    }
                    className="w-full px-3.5 py-2.5 rounded-xl bg-bg-input border border-bg-line text-sm text-white placeholder-gray-600 focus:outline-none focus:border-tactical-cyan font-mono"
                  />
                </div>
                <div>
                  <label className="block text-xs font-mono uppercase text-gray-400 mb-1">
                    Target Longitude (-180 to 180) *
                  </label>
                  <input
                    type="number"
                    step="any"
                    required
                    placeholder="e.g. 85.123456"
                    value={formData.target_lon}
                    onChange={(e) =>
                      setFormData({
                        ...formData,
                        target_lon: e.target.value === '' ? '' : Number(e.target.value),
                      })
                    }
                    className="w-full px-3.5 py-2.5 rounded-xl bg-bg-input border border-bg-line text-sm text-white placeholder-gray-600 focus:outline-none focus:border-tactical-cyan font-mono"
                  />
                </div>
              </div>

              {/* Cadence & Time of Day (IST) */}
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-mono uppercase text-gray-400 mb-1">
                    Cadence *
                  </label>
                  <select
                    value={formData.cadence}
                    onChange={(e) =>
                      setFormData({
                        ...formData,
                        cadence: e.target.value as ScheduleCadence,
                      })
                    }
                    className="w-full px-3.5 py-2.5 rounded-xl bg-bg-input border border-bg-line text-sm text-white focus:outline-none focus:border-tactical-cyan font-sans cursor-pointer"
                  >
                    <option value="daily">Daily (Every Day)</option>
                    <option value="weekly">Weekly (Selected Days)</option>
                  </select>
                </div>

                <div>
                  <label className="block text-xs font-mono uppercase text-gray-400 mb-1">
                    Time of Day (HH:MM IST) *
                  </label>
                  <input
                    type="time"
                    required
                    value={formData.time_of_day}
                    onChange={(e) =>
                      setFormData({ ...formData, time_of_day: e.target.value })
                    }
                    className="w-full px-3.5 py-2.5 rounded-xl bg-bg-input border border-bg-line text-sm text-white focus:outline-none focus:border-tactical-cyan font-mono"
                  />
                  <span className="text-[10px] text-gray-500 font-mono mt-0.5 block">
                    Local India Standard Time (Asia/Kolkata)
                  </span>
                </div>
              </div>

              {/* Days of week (only if weekly) */}
              {formData.cadence === 'weekly' && (
                <div className="p-3.5 rounded-xl bg-bg-input border border-bg-line space-y-2">
                  <label className="block text-xs font-mono uppercase text-gray-300">
                    Days of Week (Weekly Schedule) *
                  </label>
                  <div className="grid grid-cols-4 sm:grid-cols-7 gap-2">
                    {DAYS_OF_WEEK.map((d) => {
                      const isChecked = formData.days_of_week.includes(d.value);
                      return (
                        <button
                          key={d.value}
                          type="button"
                          onClick={() => handleDayToggle(d.value)}
                          className={`py-2 px-1 rounded-lg text-xs font-mono font-bold transition-all border text-center ${
                            isChecked
                              ? 'bg-tactical-purple/20 border-tactical-purple/50 text-tactical-purple'
                              : 'bg-bg-card border-bg-line text-gray-400 hover:text-white'
                          }`}
                        >
                          {d.label}
                        </button>
                      );
                    })}
                  </div>
                  {formData.days_of_week.length === 0 && (
                    <p className="text-[11px] text-tactical-warn font-mono">
                      * Please select at least one day
                    </p>
                  )}
                </div>
              )}

              {/* Cruise Alt & Loiter Seconds */}
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-mono uppercase text-gray-400 mb-1">
                    Cruise Altitude (m)
                  </label>
                  <input
                    type="number"
                    step="1"
                    min="5"
                    max="120"
                    placeholder="Optional (Default 25m)"
                    value={formData.cruise_alt_m ?? ''}
                    onChange={(e) =>
                      setFormData({
                        ...formData,
                        cruise_alt_m:
                          e.target.value === '' ? '' : Number(e.target.value),
                      })
                    }
                    className="w-full px-3.5 py-2.5 rounded-xl bg-bg-input border border-bg-line text-sm text-white placeholder-gray-600 focus:outline-none focus:border-tactical-cyan font-mono"
                  />
                </div>

                <div>
                  <label className="block text-xs font-mono uppercase text-gray-400 mb-1">
                    Loiter Duration (Seconds) *
                  </label>
                  <input
                    type="number"
                    step="5"
                    min="10"
                    required
                    value={formData.loiter_seconds}
                    onChange={(e) =>
                      setFormData({
                        ...formData,
                        loiter_seconds:
                          e.target.value === '' ? '' : Number(e.target.value),
                      })
                    }
                    className="w-full px-3.5 py-2.5 rounded-xl bg-bg-input border border-bg-line text-sm text-white focus:outline-none focus:border-tactical-cyan font-mono"
                  />
                </div>
              </div>

              {/* Active Checkbox */}
              <div className="flex items-center gap-2 pt-1">
                <input
                  type="checkbox"
                  id="activeToggle"
                  checked={formData.active}
                  onChange={(e) =>
                    setFormData({ ...formData, active: e.target.checked })
                  }
                  className="w-4 h-4 rounded bg-bg-input border-bg-line text-tactical-cyan focus:ring-0 cursor-pointer"
                />
                <label
                  htmlFor="activeToggle"
                  className="text-xs font-mono text-gray-300 cursor-pointer select-none"
                >
                  Enable schedule immediately upon saving (Active)
                </label>
              </div>

              {/* Modal Actions */}
              <div className="flex items-center justify-end gap-3 pt-3 border-t border-bg-line">
                <button
                  type="button"
                  onClick={closeModal}
                  disabled={actionLoading}
                  className="px-4 py-2 rounded-xl text-xs font-mono uppercase text-gray-400 hover:text-white hover:bg-bg-line transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={actionLoading}
                  className="flex items-center gap-2 px-5 py-2.5 rounded-xl bg-tactical-cyan text-bg font-bold text-xs uppercase tracking-wider hover:bg-tactical-cyan/90 transition-all shadow-[0_0_15px_rgba(0,229,255,0.25)]"
                >
                  {actionLoading ? (
                    <div className="w-4 h-4 border-2 border-bg border-t-transparent rounded-full animate-spin" />
                  ) : editingScheduleId ? (
                    'Update Schedule'
                  ) : (
                    'Create Schedule'
                  )}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* 4. DELETE CONFIRMATION DIALOG */}
      {deleteTarget && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4 animate-fade-in">
          <div className="bg-bg-card border border-bg-line rounded-2xl max-w-md w-full p-6 shadow-2xl space-y-4">
            <div className="flex items-center gap-3 text-tactical-bad">
              <div className="p-2 rounded-xl bg-tactical-bad/15 border border-tactical-bad/30">
                <Trash2 className="w-5 h-5" />
              </div>
              <h3 className="text-base font-bold text-white">DELETE SCHEDULE</h3>
            </div>

            <p className="text-xs text-gray-300">
              Are you sure you want to delete the schedule{' '}
              <strong className="text-white">"{deleteTarget.name}"</strong>?
              This will permanently stop recurring patrol flights for this target.
            </p>

            <div className="p-3 rounded-xl bg-bg-input border border-bg-line text-xs font-mono space-y-1 text-gray-400">
              <div>
                Cadence:{' '}
                <span className="text-tactical-cyan uppercase">
                  {deleteTarget.cadence}
                </span>{' '}
                at {deleteTarget.time_of_day.slice(0, 5)} IST
              </div>
              <div>
                Target: {deleteTarget.target_lat.toFixed(5)},{' '}
                {deleteTarget.target_lon.toFixed(5)}
              </div>
            </div>

            <div className="flex items-center justify-end gap-3 pt-2">
              <button
                type="button"
                onClick={() => setDeleteTarget(null)}
                disabled={actionLoading}
                className="px-4 py-2 rounded-xl text-xs font-mono uppercase text-gray-400 hover:text-white hover:bg-bg-line transition-colors"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleConfirmDelete}
                disabled={actionLoading}
                className="flex items-center gap-1.5 px-4 py-2 rounded-xl bg-tactical-bad text-white font-bold text-xs uppercase tracking-wider hover:bg-tactical-bad/90 transition-all shadow-[0_0_12px_rgba(224,83,61,0.3)]"
              >
                {actionLoading ? (
                  <div className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
                ) : (
                  <>
                    <Trash2 className="w-3.5 h-3.5" />
                    <span>Delete</span>
                  </>
                )}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
