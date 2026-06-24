This is a comprehensive energy system optimization pipeline for [China's NDC (Nationally Determined Contributions) 2025 modeling](https://github.com/Power-Lab/CellReports_NDC_2025/tree/main) (Zhenhua).

## **Key Stages**

### **Stage 1: Input Generation (Steps 0-1)**

* Sets up scenario parameters (emission targets, technology costs, electrification strategies, etc.)  
* Generates multi-year automation inputs covering 2025-2060 in 5-year intervals  
* Parameters include:  
  * Emission targets (2C, 1.5C scenarios)  
  * Heating electrification methods (heat pump vs CCS)  
  * Cost decline assumptions for renewables  
  * Carbon capture & storage (CCS) configurations

### **Stage 2: Data Initialization (Steps 2-3)**

* Loads scenario parameter templates from JSON  
* Initializes exogenous parameters for each year in the timeline:  
  * VRE costs: Wind (onshore/offshore), solar (utility/distributed), battery, CAES, VRB storage  
  * Optimization hours: Seeding specific days for computational efficiency  
  * Demand layers: Hourly electricity demand profiles

### **Stage 3: Endogenous Parameter Setup (Step 4\)**

* Initializes elements that depend on previous year results:  
  * VRE cell data: Wind and solar resource availability and distribution  
  * Layer capacity: Initial capacity constraints for the optimization model

### **Stage 4: Optimization & Post-Processing (Steps 5-7)**

* Runs optimization for each target year (2030, 2035\) via interProvinModel()  
* Post-processes outputs:  
  * Cell resource information extraction  
  * Transmission capacity analysis  
  * Storage capacity updates  
  * Transmission info compilation  
  * Curtailment analysis for wind and solar

### **Stage 5: Aggregation & Cleanup (Steps 8-9)**

* Summarizes outputs at provincial and national levels across all simulated years  
* Deletes large temporary files (solar/wind cell pickles) to manage storage

## **Key Configuration Parameters**

* Optimization window: 7 days per year  
* Simulation period: 2030-2035 (2 years modeled)  
* Cost trajectories: Baseline, conservative, or specialized scenarios (low wind, low battery)  
* Regional resolution: Provincial/cell-level analysis across China

This pipeline enables scenario-based long-term energy planning with detailed technology costs, renewable integration, and decarbonization pathways.  
