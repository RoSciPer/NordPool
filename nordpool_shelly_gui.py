#!/usr/bin/env python3
"""
Nord Pool Spot Prices GUI Application with Shelly control
Fetches and displays current and upcoming hourly electricity spot prices for Latvia (LV)
from the Nord Pool public API and allows manual/automatic Shelly relay control.
"""

import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import tkinter as tk
from tkinter import ttk, messagebox


class NordPoolPricesApp:
    """Application for displaying Nord Pool spot prices and controlling a Shelly device."""

    CHEAP_THRESHOLD = 0.04  # EUR/kWh
    EXPENSIVE_THRESHOLD = 0.10  # EUR/kWh
    SUPER_CHEAP_THRESHOLD = 0.02  # EUR/kWh
    MIN_DAILY_ON_HOURS = 6
    MIN_NIGHT_ON_HOURS = 3
    NIGHT_END_HOUR = 7
    DAY_PERIOD_START = 10
    DAY_PERIOD_END = 17

    def __init__(self, root):
        self.root = root
        self.root.title("Nord Pool Spot Prices & Shelly Control")
        self.root.geometry("950x860")
        self.local_tz = ZoneInfo("Europe/Riga")

        self.api_url = "https://dashboard.elering.ee/api/nps/price"
        self.params = self.get_query_params()
        self.prices = {}
        self.shelly_last_auto_action = None
        self.session = requests.Session()

        self.setup_ui()
        self.refresh_prices()

    def get_query_params(self):
        """Build query interval in UTC from Riga-local day boundaries."""
        now_local = datetime.now(self.local_tz)
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(days=2) - timedelta(seconds=1)

        return {
            "start": start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    def setup_ui(self):
        """Setup the user interface."""
        control_frame = ttk.Frame(self.root)
        control_frame.pack(fill=tk.X, padx=10, pady=10)

        title = ttk.Label(
            control_frame,
            text="Nord Pool Spot Prices - Latvia (LV)",
            font=("Arial", 14, "bold"),
        )
        title.pack(side=tk.LEFT)

        self.refresh_btn = ttk.Button(control_frame, text="↻ Refresh", command=self.on_refresh)
        self.refresh_btn.pack(side=tk.RIGHT)

        self.show_hourly_average = tk.BooleanVar(value=False)
        avg_price_checkbox = ttk.Checkbutton(
            control_frame,
            text="Show Hourly Average (from 4×15min)",
            variable=self.show_hourly_average,
            command=self.display_prices,
        )
        avg_price_checkbox.pack(side=tk.RIGHT, padx=6)

        self.status_label = ttk.Label(self.root, text="Loading...", foreground="blue", font=("Arial", 10))
        self.status_label.pack(fill=tk.X, padx=10)

        legend_frame = ttk.LabelFrame(self.root, text="Price Range Legend")
        legend_frame.pack(fill=tk.X, padx=10, pady=5)

        # Dynamic legend based on current prices
        if self.prices:
            prices_for_legend = self.get_hourly_average_prices() if self.show_hourly_average.get() else self.prices
            cheap_thresh, expensive_thresh = self.get_dynamic_thresholds(prices_for_legend)
            legend_text = (
                f"🟢 Cheap: < {cheap_thresh:.3f} EUR/kWh  |  "
                f"⚪ Normal: {cheap_thresh:.3f}-{expensive_thresh:.3f} EUR/kWh  |  "
                f"🔴 Expensive: > {expensive_thresh:.3f} EUR/kWh  |  "
                "▶ NOW: current slot"
            )
        else:
            legend_text = (
                "🟢 Cheap: < 0.04 EUR/kWh  |  "
                "⚪ Normal: 0.04-0.10 EUR/kWh  |  "
                "🔴 Expensive: > 0.10 EUR/kWh  |  "
                "▶ NOW: current slot"
            )
        ttk.Label(legend_frame, text=legend_text, font=("Arial", 9)).pack(padx=5, pady=5)

        self.setup_shelly_controls()
        self.setup_price_table()

    def setup_shelly_controls(self):
        """Create Shelly control UI."""
        shelly_frame = ttk.LabelFrame(self.root, text="Shelly Switch Control")
        shelly_frame.pack(fill=tk.X, padx=10, pady=5)

        self.shelly_host_var = tk.StringVar(value="192.168.1.100")
        self.shelly_user_var = tk.StringVar()
        self.shelly_pass_var = tk.StringVar()
        self.shelly_auto_var = tk.BooleanVar(value=False)

        host_row = ttk.Frame(shelly_frame)
        host_row.pack(fill=tk.X, padx=5, pady=4)
        ttk.Label(host_row, text="Shelly host:", width=14).pack(side=tk.LEFT)
        ttk.Entry(host_row, textvariable=self.shelly_host_var, width=24).pack(side=tk.LEFT)
        ttk.Label(host_row, text="User:", width=8).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Entry(host_row, textvariable=self.shelly_user_var, width=16).pack(side=tk.LEFT)
        ttk.Label(host_row, text="Pass:", width=8).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Entry(host_row, textvariable=self.shelly_pass_var, show="*", width=16).pack(side=tk.LEFT)

        button_row = ttk.Frame(shelly_frame)
        button_row.pack(fill=tk.X, padx=5, pady=4)
        self.on_btn = ttk.Button(button_row, text="Shelly ON", command=lambda: self.start_thread(self.control_shelly, "on"))
        self.on_btn.pack(side=tk.LEFT, padx=6)
        self.off_btn = ttk.Button(button_row, text="Shelly OFF", command=lambda: self.start_thread(self.control_shelly, "off"))
        self.off_btn.pack(side=tk.LEFT, padx=6)
        ttk.Button(button_row, text="Refresh Shelly", command=lambda: self.start_thread(self.update_shelly_status)).pack(side=tk.LEFT, padx=6)

        auto_row = ttk.Frame(shelly_frame)
        auto_row.pack(fill=tk.X, padx=5, pady=4)
        ttk.Checkbutton(
            auto_row,
            text="Automatic Shelly control based on price",
            variable=self.shelly_auto_var,
            command=self.on_auto_toggle,
        ).pack(side=tk.LEFT)
        ttk.Label(auto_row, text="(ON if cheap, OFF if expensive)").pack(side=tk.LEFT, padx=8)

        self.shelly_status_label = ttk.Label(shelly_frame, text="Shelly status: unknown", font=("Arial", 10))
        self.shelly_status_label.pack(fill=tk.X, padx=6, pady=(3, 4))

        self.shelly_schedule_label = ttk.Label(shelly_frame, text="Shelly schedule: not yet computed", font=("Arial", 10))
        self.shelly_schedule_label.pack(fill=tk.X, padx=6, pady=(0, 4))

    def setup_price_table(self):
        """Create the price table."""
        table_frame = ttk.Frame(self.root)
        table_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        columns = ("Time", "Price (EUR/kWh)", "Status")
        self.tree = ttk.Treeview(table_frame, columns=columns, height=30, show="headings")
        self.tree.column("Time", anchor=tk.W, width=220)
        self.tree.column("Price (EUR/kWh)", anchor=tk.CENTER, width=220)
        self.tree.column("Status", anchor=tk.CENTER, width=140)
        for col in columns:
            self.tree.heading(col, text=col)

        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscroll=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)

        self.tree.tag_configure("cheap", background="#90EE90", foreground="#000000")
        self.tree.tag_configure("expensive", background="#FFB6C6", foreground="#000000")
        self.tree.tag_configure("normal", background="white", foreground="#000000")
        self.tree.tag_configure("current", font=("TkDefaultFont", 10, "bold"), background="#FFFFCC", foreground="#000000")
        self.tree.tag_configure("below_zero_blue", background="#1E3A8A", foreground="#FFFFFF")
        self.tree.tag_configure("green_0", background="#14532D", foreground="#FFFFFF")
        self.tree.tag_configure("green_1", background="#166534", foreground="#FFFFFF")
        self.tree.tag_configure("green_2", background="#15803D", foreground="#FFFFFF")
        self.tree.tag_configure("green_3", background="#16A34A", foreground="#FFFFFF")
        self.tree.tag_configure("yellow_0", background="#FEF9C3", foreground="#000000")
        self.tree.tag_configure("yellow_1", background="#FEF08A", foreground="#000000")
        self.tree.tag_configure("yellow_2", background="#FDE047", foreground="#000000")
        self.tree.tag_configure("yellow_3", background="#FACC15", foreground="#000000")
        self.tree.tag_configure("yellow_4", background="#EAB308", foreground="#000000")
        self.tree.tag_configure("yellow_5", background="#F59E0B", foreground="#000000")
        self.tree.tag_configure("red_0", background="#FCA5A5", foreground="#000000")
        self.tree.tag_configure("red_1", background="#F87171", foreground="#000000")
        self.tree.tag_configure("red_2", background="#EF4444", foreground="#FFFFFF")
        self.tree.tag_configure("red_3", background="#DC2626", foreground="#FFFFFF")
        self.tree.tag_configure("red_4", background="#B91C1C", foreground="#FFFFFF")
        self.tree.tag_configure("brown", background="#8B4513", foreground="#FFFFFF")

    def fetch_from_api(self):
        """Fetch price data from Nord Pool API."""
        try:
            response = self.session.get(self.api_url, params=self.params, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.Timeout:
            return {"error": "Request timeout (>10s). Please check your internet connection and try again."}
        except requests.exceptions.ConnectionError:
            return {"error": "Connection error. Please check your internet connection."}
        except requests.exceptions.HTTPError as e:
            return {"error": f"HTTP Error {e.response.status_code}: {e.response.reason}"}
        except requests.exceptions.RequestException as e:
            return {"error": f"Request failed: {str(e)}"}
        except Exception as e:
            return {"error": f"Unexpected error: {str(e)}"}

    def parse_prices(self, data):
        """Parse 15-minute prices from API response."""
        if "error" in data:
            return None

        try:
            country_data = data.get("data", {}).get("lv", [])
            if not country_data:
                messagebox.showwarning("Warning", "No pricing data available for Latvia")
                return None

            prices = {}
            for entry in country_data:
                timestamp = entry.get("timestamp")
                price = entry.get("price")
                if timestamp is not None and price is not None:
                    time_key = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(self.local_tz)
                    time_key = time_key.replace(second=0, microsecond=0)
                    prices[time_key] = float(price) / 1000.0

            return prices if prices else None
        except Exception as e:
            messagebox.showerror("Parse Error", f"Error parsing API response: {str(e)}")
            return None

    def get_hourly_average_prices(self):
        """Aggregate 15-minute prices into hourly averages."""
        hourly_prices = {}
        for time_dt, price in self.prices.items():
            hour_key = time_dt.replace(minute=0, second=0, microsecond=0)
            hourly_prices.setdefault(hour_key, []).append(price)
        return {hour: sum(values) / len(values) for hour, values in hourly_prices.items()}

    def build_shelly_schedule(self):
        """Build the daily Shelly on/off schedule from hourly prices."""
        hourly_prices = self.get_hourly_average_prices()
        today = datetime.now(self.local_tz).date()
        today_prices = {dt: price for dt, price in hourly_prices.items() if dt.date() == today}
        if not today_prices:
            return set()

        selected = {dt for dt, price in today_prices.items() if price <= self.SUPER_CHEAP_THRESHOLD}

        night_candidates = sorted(
            [(price, dt) for dt, price in today_prices.items() if dt.hour < self.NIGHT_END_HOUR and dt not in selected],
            key=lambda item: item[0],
        )
        day_candidates = sorted(
            [(price, dt) for dt, price in today_prices.items() if self.DAY_PERIOD_START <= dt.hour < self.DAY_PERIOD_END and dt not in selected],
            key=lambda item: item[0],
        )
        other_candidates = sorted(
            [
                (price, dt)
                for dt, price in today_prices.items()
                if dt not in selected and dt not in {item[1] for item in night_candidates + day_candidates}
            ],
            key=lambda item: item[0],
        )

        night_selected = [dt for dt in selected if dt.hour < self.NIGHT_END_HOUR]
        if len(night_selected) < self.MIN_NIGHT_ON_HOURS:
            for price, dt in night_candidates:
                selected.add(dt)
                night_selected.append(dt)
                if len(night_selected) >= self.MIN_NIGHT_ON_HOURS:
                    break

        while len(selected) < self.MIN_DAILY_ON_HOURS and day_candidates:
            selected.add(day_candidates.pop(0)[1])
        while len(selected) < self.MIN_DAILY_ON_HOURS and night_candidates:
            selected.add(night_candidates.pop(0)[1])
        while len(selected) < self.MIN_DAILY_ON_HOURS and other_candidates:
            selected.add(other_candidates.pop(0)[1])

        return selected

    def format_shelly_schedule_summary(self, selected_hours):
        """Format a human-readable summary for the selected Shelly schedule."""
        night_count = sum(1 for dt in selected_hours if dt.hour < self.NIGHT_END_HOUR)
        day_count = sum(1 for dt in selected_hours if self.DAY_PERIOD_START <= dt.hour < self.DAY_PERIOD_END)
        total = len(selected_hours)
        return (
            f"Shelly schedule: {total}h on today ({night_count}h night, {day_count}h day)"
            if total
            else "Shelly schedule: no hours selected"
        )

    def get_dynamic_thresholds(self, prices):
        """Calculate dynamic thresholds based on daily average price."""
        if not prices:
            return self.CHEAP_THRESHOLD, self.EXPENSIVE_THRESHOLD
        
        price_values = list(prices.values())
        avg_price = sum(price_values) / len(price_values)
        
        # Cheap: 30% below average, Expensive: 30% above average
        cheap_threshold = avg_price * 0.7
        expensive_threshold = avg_price * 1.3
        
        return cheap_threshold, expensive_threshold

    def get_dynamic_color_tag(self, price):
        """Determine dynamic color tag based on price range."""
        if price < 0:
            return "below_zero_blue"
        
        # Use dynamic thresholds if we have prices
        if self.prices:
            cheap_thresh, expensive_thresh = self.get_dynamic_thresholds(self.get_hourly_average_prices() if self.show_hourly_average.get() else self.prices)
        else:
            cheap_thresh, expensive_thresh = self.CHEAP_THRESHOLD, self.EXPENSIVE_THRESHOLD
        
        if price < cheap_thresh:
            green_index = int((price / cheap_thresh) * 2)  # Scale to 0-2
            green_index = min(max(green_index, 0), 3)
            return f"green_{green_index}"
        if price < expensive_thresh:
            yellow_index = int(((price - cheap_thresh) / (expensive_thresh - cheap_thresh)) * 6)
            yellow_index = min(max(yellow_index, 0), 5)
            return f"yellow_{yellow_index}"
        if price < expensive_thresh * 1.5:
            red_index = int(((price - expensive_thresh) / (expensive_thresh * 0.5)) * 5)
            red_index = min(max(red_index, 0), 4)
            return f"red_{red_index}"
        return "brown"

    def display_prices(self):
        """Display prices in the table."""
        for item in self.tree.get_children():
            self.tree.delete(item)

        if not self.prices:
            self.tree.insert("", tk.END, values=("No data available", "", ""), tags=("normal",))
            return

        prices_to_show = self.get_hourly_average_prices() if self.show_hourly_average.get() else self.prices
        now_local = datetime.now(self.local_tz)
        current_hour = now_local.replace(minute=0, second=0, microsecond=0)
        current_quarter = now_local.replace(minute=(now_local.minute // 15) * 15, second=0, microsecond=0)

        sorted_items = sorted(prices_to_show.items())
        price_values = [p for p in prices_to_show.values()]
        min_price = min(price_values) if price_values else 0
        max_price = max(price_values) if price_values else 0
        avg_price = sum(price_values) / len(price_values) if price_values else 0

        for time_dt, price in sorted_items:
            time_str = time_dt.strftime("%Y-%m-%d %H:%M")
            price_str = f"{price:.4f}"
            if self.show_hourly_average.get():
                is_current = time_dt == current_hour
            else:
                is_current = time_dt == current_quarter
            status_str = "▶ NOW" if is_current else ""
            color_tag = self.get_dynamic_color_tag(price)
            self.tree.insert("", tk.END, values=(time_str, price_str, status_str), tags=(color_tag,))

        mode_text = "Mode: Hourly average" if self.show_hourly_average.get() else "Mode: 15-minute"
        stats_text = (
            f"{mode_text} | Min: {min_price:.4f} EUR/kWh | "
            f"Max: {max_price:.4f} EUR/kWh | Avg: {avg_price:.4f} EUR/kWh"
        )
        self.status_label.config(text=stats_text, foreground="darkblue")

        if self.shelly_auto_var.get():
            selected_hours = self.build_shelly_schedule()
            self.shelly_schedule_label.config(text=self.format_shelly_schedule_summary(selected_hours))
            self.auto_control_shelly(selected_hours)

    def get_shelly_base_url(self):
        host = self.shelly_host_var.get().strip()
        if not host:
            raise ValueError("Shelly host must be provided")
        if not host.startswith("http://") and not host.startswith("https://"):
            host = f"http://{host}"
        return host.rstrip("/")

    def get_shelly_auth(self):
        user = self.shelly_user_var.get().strip()
        password = self.shelly_pass_var.get().strip()
        return (user, password) if user and password else None

    def control_shelly(self, action):
        """Send a command to the Shelly relay."""
        try:
            base_url = self.get_shelly_base_url()
            auth = self.get_shelly_auth()
            if action not in ("on", "off"):
                raise ValueError("Invalid Shelly action")

            url = f"{base_url}/rpc/relay/0?turn={action}"
            response = self.session.get(url, auth=auth, timeout=10)
            response.raise_for_status()
            self.shelly_status_label.config(text=f"Shelly {action.upper()} command sent successfully", foreground="green")
            self.update_shelly_status()
        except Exception as e:
            self.shelly_status_label.config(text=f"Shelly control failed: {e}", foreground="red")

    def update_shelly_status(self):
        """Fetch current Shelly relay status."""
        try:
            base_url = self.get_shelly_base_url()
            auth = self.get_shelly_auth()
            status_url = f"{base_url}/status"
            response = self.session.get(status_url, auth=auth, timeout=10)
            response.raise_for_status()
            data = response.json()
            relay_state = data.get("relays", [{}])[0].get("ison")
            if relay_state is True:
                status_text = "Shelly relay is ON"
                status_color = "green"
            elif relay_state is False:
                status_text = "Shelly relay is OFF"
                status_color = "orange"
            else:
                status_text = "Shelly status unknown"
                status_color = "black"
            self.shelly_status_label.config(text=status_text, foreground=status_color)
        except Exception as e:
            self.shelly_status_label.config(text=f"Shelly status error: {e}", foreground="red")

    def auto_control_shelly(self, selected_hours=None):
        """Automatically control Shelly based on the computed daily schedule."""
        if not self.prices:
            return

        if selected_hours is None:
            selected_hours = self.build_shelly_schedule()

        now_local = datetime.now(self.local_tz)
        current_hour = now_local.replace(minute=0, second=0, microsecond=0)
        current_price = self.get_hourly_average_prices().get(current_hour)

        desired_action = "on" if current_hour in selected_hours else "off"

        if current_price is not None and current_price <= self.SUPER_CHEAP_THRESHOLD:
            desired_action = "on"

        if desired_action == self.shelly_last_auto_action:
            return

        text = f"Auto control: turning Shelly {desired_action.upper()}"
        if current_price is not None:
            text += f" (price {current_price:.4f})"
        self.shelly_last_auto_action = desired_action
        self.shelly_status_label.config(text=text, foreground="darkblue")
        self.start_thread(self.control_shelly, desired_action)

    def on_refresh(self):
        """Handle refresh button click."""
        thread = threading.Thread(target=self.refresh_prices, daemon=True)
        thread.start()

    def on_auto_toggle(self):
        if self.shelly_auto_var.get():
            self.shelly_status_label.config(text="Automatic Shelly control enabled", foreground="darkblue")
            self.display_prices()
        else:
            self.shelly_status_label.config(text="Automatic Shelly control disabled", foreground="black")
            self.shelly_schedule_label.config(text="Shelly schedule: automatic mode off")

    def start_thread(self, target, *args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()

    def refresh_prices(self):
        self.refresh_btn.config(state=tk.DISABLED)
        self.status_label.config(text="Fetching data from Nord Pool API...", foreground="blue")
        self.root.update()

        self.params = self.get_query_params()
        data = self.fetch_from_api()

        if "error" in data:
            messagebox.showerror("API Error", data["error"])
            self.status_label.config(text="Error fetching data", foreground="red")
            self.refresh_btn.config(state=tk.NORMAL)
            return

        self.prices = self.parse_prices(data)
        if self.prices is None:
            self.status_label.config(text="Failed to parse pricing data", foreground="red")
            self.refresh_btn.config(state=tk.NORMAL)
            return

        self.display_prices()
        self.refresh_btn.config(state=tk.NORMAL)
        self.start_thread(self.update_shelly_status)


def main():
    root = tk.Tk()
    app = NordPoolPricesApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
