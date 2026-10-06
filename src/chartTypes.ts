import type { IChartApiBase, ISeriesApi, MouseEventParams, SeriesType } from 'lightweight-charts';
import type { Theme } from './types';

export interface ChartSeriesInfo {
  series: ISeriesApi<SeriesType, number>;
  name: string;
  color: string;
  hidden?: boolean;
}
export interface ChartBuildResult {
  series: ChartSeriesInfo[];
  overlays?: readonly unknown[];
  cleanup?: () => void;
  tooltip?: (event: MouseEventParams<number>) => string[];
}
export interface LightweightChartProps {
  label: string;
  theme: Theme;
  height?: number;
  axis?: 'time' | 'number';
  revision?: string | number;
  build: (chart: IChartApiBase<number>) => ChartBuildResult;
  xFormat?: (value: number) => string;
  range?: [number, number];
  rangeKey?: string | number;
  onRangeChange?: (range: { from: number; to: number }) => void;
  onPointClick?: (x: number, y: number | undefined) => void;
  onDoubleClick?: () => void;
  xTitle?: string;
  yTitle?: string;
  legend?: boolean;
}
