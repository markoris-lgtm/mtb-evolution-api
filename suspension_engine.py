import json
from typing import Optional, Literal, List, Dict, Any
from pydantic import BaseModel, Field, field_validator
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="MTB Evolution API", version="2.0 - Smart Context")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 1. MODELI ---
class RidingConditions(BaseModel):
    terrain: Literal['flow', 'jumps', 'technical_roots', 'mix'] = 'mix'
    weather: Literal['dry', 'wet', 'mix'] = 'dry'

class SuspensionComponent(BaseModel):
    brand: Literal['rockshox', 'fox', 'marzocchi', 'ohlins', 'other']
    travel_mm: int = Field(..., gt=35, le=250)
    has_air_spring: bool = True
    
    # Detaljne opcije
    has_rebound: bool = True
    has_lsc: bool = False
    has_hsc: bool = False
    has_lsr: bool = False
    has_hsr: bool = False
    tokens_adjustable: bool = True

    current_psi: Optional[int] = None

class RiderProfile(BaseModel):
    weight_kg: float = Field(..., gt=30, le=150)
    bike_type: Literal['hardtail', 'full_suspension_xc', 'full_suspension_trail_enduro', 'downhill']
    skill_level: str = 'intermediate'

class BikeSetup(BaseModel):
    rider: RiderProfile
    conditions: RidingConditions = RidingConditions()
    fork: SuspensionComponent
    shock: Optional[SuspensionComponent] = None

    @field_validator('shock')
    def validate_shock(cls, v, values):
        if values.data.get('rider').bike_type == 'hardtail' and v is not None:
            raise ValueError("Hardtail ne može imati zadnji shock.")
        return v

# --- 2. LOGIKA ---
class SuspensionCalculator:
    def __init__(self, specs_file='manufacturer_specs.json'):
        try:
            with open(specs_file, 'r', encoding='utf-8') as f:
                self.specs = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            print("Greška pri učitavanju specifikacija.")
            self.specs = {}

    def _get_dynamic_rebound(self, component_data: dict, weight: float) -> int:
        table = component_data.get('rebound_table', [])
        if not table: return 6
        for entry in table:
            if weight <= entry['max_kg']: return entry['clicks']
        return table[-1]['clicks']

    def calculate_baseline(self, setup: BikeSetup):
        rider_weight = setup.rider.weight_kg
        bike_type = setup.rider.bike_type
        conditions = setup.conditions
        recommendations = {}

        modifiers = {
            "terrain": {
                "flow": {"lsc": 1, "rebound": 0, "psi_pct": 1.0, "tip": "Flow staza traži podršku."},
                "jumps": {"lsc": 2, "hsc": 1, "rebound": 1, "psi_pct": 1.05, "tip": "Skokovi traže stabilnost i sporiji rebound."},
                "technical_roots": {"lsc": -1, "rebound": -1, "psi_pct": 0.98, "tip": "Korijenje traži mekoću i brz rebound."},
                "mix": {"lsc": 0, "rebound": 0, "psi_pct": 1.0, "tip": "Balansirano."}
            },
            "weather": {
                "dry": {"lsc": 0, "rebound": 0, "psi_pct": 1.0, "tip": ""},
                "wet": {"lsc": -2, "rebound": -1, "psi_pct": 0.95, "tip": "Mokro je! Mekša kompresija za grip."},
                "mix": {"lsc": -1, "rebound": 0, "psi_pct": 0.98, "tip": "Promjenjivo."}
            }
        }

        t_mod = modifiers["terrain"][conditions.terrain]
        w_mod = modifiers["weather"][conditions.weather]

        def get_component_settings(comp: SuspensionComponent, comp_type: str):
            specs_section = self.specs.get(comp_type + 's', {})
            brand_data = specs_section.get(comp.brand, {})
            if not brand_data:
                fallback_key = 'rockshox' if comp_type == 'fork' else 'standard_air'
                brand_data = specs_section.get(fallback_key, {})

            psi_mult = brand_data.get('psi_multiplier', 1.0)
            base_psi = int(rider_weight * psi_mult)
            final_psi = int(base_psi * t_mod.get('psi_pct', 1.0) * w_mod.get('psi_pct', 1.0))

            sag_pct = self.specs.get('sag_targets', {}).get(bike_type, {}).get(comp_type, 0.25)
            sag_mm = int(comp.travel_mm * sag_pct)

            base_rebound = self._get_dynamic_rebound(brand_data, rider_weight)
            base_lsc = brand_data.get('base_lsc', 2)
            base_hsc = brand_data.get('base_hsc', 1)

            final_rebound = max(0, base_rebound + t_mod.get('rebound', 0) + w_mod.get('rebound', 0))
            final_lsr = final_rebound
            final_hsr = max(0, final_rebound + 1)

            final_lsc = max(0, base_lsc + t_mod.get('lsc', 0) + w_mod.get('lsc', 0))
            final_hsc = max(0, base_hsc + t_mod.get('hsc', 0) + w_mod.get('hsc', 0))

            clicks = {}
            if comp.has_rebound: clicks['rebound'] = final_rebound
            if comp.has_lsr: clicks['lsr'] = final_lsr
            if comp.has_hsr: clicks['hsr'] = final_hsr
            if comp.has_lsc: clicks['lsc'] = final_lsc
            if comp.has_hsc: clicks['hsc'] = final_hsc

            return {
                "psi": final_psi,
                "sag_mm": sag_mm,
                "sag_pct": int(sag_pct * 100),
                "clicks": clicks,
                "smart_tip": f"{t_mod['tip']} {w_mod['tip']}".strip()
            }

        recommendations['fork'] = get_component_settings(setup.fork, 'fork')
        if setup.shock:
            recommendations['shock'] = get_component_settings(setup.shock, 'shock')
        
        return recommendations

# --- 3. DIJAGNOSTIKA ---
class DiagnosticAssistant:
    def __init__(self, logic_file='suspension_logic.json'):
        try:
            with open(logic_file, 'r', encoding='utf-8') as f:
                self.logic_db = json.load(f)
        except Exception:
            self.logic_db = []

    def diagnose_problem(self, setup: BikeSetup, symptom_id: str):
        problem = next((item for item in self.logic_db if item["symptom_id"] == symptom_id), None)
        if not problem: return {"error": "Nepoznat simptom."}

        comp_name = problem['required_component']
        actual_comp = setup.fork if comp_name == 'fork' else setup.shock
        if not actual_comp: return {"error": "Komponenta ne postoji na biciklu."}

        valid_solutions = []
        for solution in problem['solutions']:
            check = solution['logic_check']
            if check == "always_true" or getattr(actual_comp, check, False):
                valid_solutions.append(solution)

        valid_solutions.sort(key=lambda x: x['priority'])
        if not valid_solutions: return {"result": "Nema rješenja, posjeti servis."}

        return {
            "symptom": problem['symptom_name'],
            "primary_fix": valid_solutions[0],
            "secondary_fix": valid_solutions[1] if len(valid_solutions) > 1 else None
        }

# --- 4. API ---
@app.post("/api/calculate-setup")
async def get_baseline_setup(bike: BikeSetup):
    return SuspensionCalculator().calculate_baseline(bike)

@app.post("/api/diagnose")
async def diagnose_issue(bike: BikeSetup, symptom_id: str):
    return DiagnosticAssistant().diagnose_problem(bike, symptom_id)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)