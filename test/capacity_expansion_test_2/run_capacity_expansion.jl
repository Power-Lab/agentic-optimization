using CSV
using DataFrames
using HiGHS
using JuMP

struct ScenarioData
    generators::DataFrame
    demand::DataFrame
    G::Vector{String}
    H::Vector{Int}
    demand_by_hour::Dict{Int,Float64}
    fixed_cost::Dict{String,Float64}
    var_cost::Dict{String,Float64}
    description::Dict{String,String}
end

function repo_root()
    return normpath(joinpath(@__DIR__, "..", ".."))
end

function resolve_repo_path(root::AbstractString, path::AbstractString)
    return isabspath(path) ? normpath(path) : normpath(joinpath(root, path))
end

function require_columns(df::DataFrame, required_columns, label::AbstractString)
    missing = setdiff(String.(required_columns), String.(names(df)))
    if !isempty(missing)
        missing_text = join(missing, ", ")
        error("$(label) is missing required columns: $(missing_text)")
    end
end

function load_config(config_path::AbstractString)
    if !isfile(config_path)
        error("Scenario config not found: $(config_path)")
    end

    # Keep this script runnable in the notebook environment without requiring YAML.jl.
    # The fields below mirror scenario_config.yaml and are the subset needed to solve
    # and export the thermal-only capacity expansion case.
    return Dict(
        "scenario_id" => "capacity_expansion_test_2_thermal_greenfield",
        "input_data_files" => Dict(
            "generators" => Dict(
                "path" => "doc/expansion_data/generators_for_expansion.csv",
                "required_columns" => [
                    "G", "Description", "Capex", "FixedOM", "VarOM", "HeatRate",
                    "FuelCost", "WACC", "AssetLife", "CRF", "Annuity", "FixedCost", "VarCost",
                ],
            ),
            "demand" => Dict(
                "path" => "doc/expansion_data/demand_for_expansion.csv",
                "required_columns" => ["Hour", "Demand"],
            ),
        ),
        "candidate_generator_filter" => Dict(
            "include_values" => ["Geo", "Coal", "CCGT", "CT"],
            "exclude_values" => ["Wind", "Solar"],
        ),
        "sets" => Dict(
            "H" => Dict("expected_count" => 8760),
        ),
        "parameters" => Dict(
            "timestep_hours" => Dict("value" => 1.0),
            "non_served_energy_cost" => Dict("value" => 9000.0),
        ),
        "solver_settings" => Dict(
            "time_limit_seconds" => 600.0,
            "presolve" => "on",
            "log_to_console" => true,
            "log_file" => "test/capacity_expansion_test_2/highs_thermal_greenfield.log",
            "options" => Dict(
                "primal_feasibility_tolerance" => 1.0e-7,
                "dual_feasibility_tolerance" => 1.0e-7,
            ),
            "expected_statuses" => ["OPTIMAL"],
        ),
        "expected_output_files" => Dict(
            "capacity_build" => Dict("path" => "test/capacity_expansion_test_2/capacity_build.csv"),
            "hourly_generation" => Dict("path" => "test/capacity_expansion_test_2/hourly_generation.csv"),
            "non_served_energy" => Dict("path" => "test/capacity_expansion_test_2/non_served_energy.csv"),
            "solve_summary" => Dict("path" => "test/capacity_expansion_test_2/solve_summary.json"),
        ),
    )
end

function json_escape(value::AbstractString)
    escaped = replace(value, "\\" => "\\\\")
    escaped = replace(escaped, "\"" => "\\\"")
    escaped = replace(escaped, "\n" => "\\n")
    escaped = replace(escaped, "\r" => "\\r")
    escaped = replace(escaped, "\t" => "\\t")
    return escaped
end

function write_json_value(io::IO, value; indent::Int=0)
    pad = repeat(" ", indent)
    next_pad = repeat(" ", indent + 2)

    if value === nothing
        print(io, "null")
    elseif value isa AbstractString
        print(io, "\"", json_escape(value), "\"")
    elseif value isa Bool
        print(io, value ? "true" : "false")
    elseif value isa Number
        print(io, value)
    elseif value isa AbstractVector
        print(io, "[")
        for (idx, item) in enumerate(value)
            if idx > 1
                print(io, ",")
            end
            print(io, "\n", next_pad)
            write_json_value(io, item; indent=indent + 2)
        end
        if !isempty(value)
            print(io, "\n", pad)
        end
        print(io, "]")
    elseif value isa AbstractDict
        keys_sorted = sort(collect(keys(value)); by=string)
        print(io, "{")
        for (idx, key) in enumerate(keys_sorted)
            if idx > 1
                print(io, ",")
            end
            print(io, "\n", next_pad, "\"", json_escape(string(key)), "\": ")
            write_json_value(io, value[key]; indent=indent + 2)
        end
        if !isempty(keys_sorted)
            print(io, "\n", pad)
        end
        print(io, "}")
    else
        print(io, "\"", json_escape(string(value)), "\"")
    end
end

function write_json_file(path::AbstractString, data::AbstractDict)
    open(path, "w") do io
        write_json_value(io, data; indent=0)
        println(io)
    end
end

function read_inputs(config::Dict, root::AbstractString)
    generator_cfg = config["input_data_files"]["generators"]
    demand_cfg = config["input_data_files"]["demand"]

    generator_path = resolve_repo_path(root, generator_cfg["path"])
    demand_path = resolve_repo_path(root, demand_cfg["path"])

    if !isfile(generator_path)
        error("Generator data file not found: $(generator_path)")
    end
    if !isfile(demand_path)
        error("Demand data file not found: $(demand_path)")
    end

    generators_all = DataFrame(CSV.File(generator_path))
    demand = DataFrame(CSV.File(demand_path))

    require_columns(generators_all, generator_cfg["required_columns"], "Generator data")
    require_columns(demand, demand_cfg["required_columns"], "Demand data")

    include_values = String.(config["candidate_generator_filter"]["include_values"])
    exclude_values = String.(config["candidate_generator_filter"]["exclude_values"])

    generators = filter(row -> String(row.G) in include_values, generators_all)
    if nrow(generators) != length(include_values)
        found = Set(String.(generators.G))
        missing = setdiff(include_values, collect(found))
        missing_text = join(missing, ", ")
        error("Thermal candidate filter did not find expected generators: $(missing_text)")
    end

    excluded_selected = intersect(Set(String.(generators.G)), Set(exclude_values))
    if !isempty(excluded_selected)
        excluded_text = join(collect(excluded_selected), ", ")
        error("Excluded resources survived generator filter: $(excluded_text)")
    end

    expected_hours = Int(config["sets"]["H"]["expected_count"])
    if nrow(demand) != expected_hours
        error("Expected $(expected_hours) demand rows, found $(nrow(demand))")
    end

    G = String.(generators.G)
    H = Int.(demand.Hour)

    demand_by_hour = Dict(Int(row.Hour) => Float64(row.Demand) for row in eachrow(demand))
    fixed_cost = Dict(String(row.G) => Float64(row.FixedCost) for row in eachrow(generators))
    var_cost = Dict(String(row.G) => Float64(row.VarCost) for row in eachrow(generators))
    description = Dict(String(row.G) => String(row.Description) for row in eachrow(generators))

    return ScenarioData(generators, demand, G, H, demand_by_hour, fixed_cost, var_cost, description)
end

function apply_solver_settings!(model::Model, config::Dict, root::AbstractString)
    settings = config["solver_settings"]

    if haskey(settings, "time_limit_seconds")
        set_optimizer_attribute(model, "time_limit", Float64(settings["time_limit_seconds"]))
    end
    if haskey(settings, "presolve")
        set_optimizer_attribute(model, "presolve", String(settings["presolve"]))
    end
    if haskey(settings, "log_to_console") && !Bool(settings["log_to_console"])
        set_silent(model)
    end
    if haskey(settings, "log_file")
        log_path = resolve_repo_path(root, settings["log_file"])
        mkpath(dirname(log_path))
        set_optimizer_attribute(model, "log_file", log_path)
    end

    options = get(settings, "options", Dict())
    for (name, value) in options
        set_optimizer_attribute(model, String(name), value)
    end
end

function build_model(config::Dict, data::ScenarioData, root::AbstractString)
    model = Model(HiGHS.Optimizer)
    apply_solver_settings!(model, config, root)

    G = data.G
    H = data.H
    dt = Float64(config["parameters"]["timestep_hours"]["value"])
    nse_cost = Float64(config["parameters"]["non_served_energy_cost"]["value"])

    @variable(model, CAP[g in G] >= 0)
    @variable(model, GEN[g in G, h in H] >= 0)
    @variable(model, NSE[h in H] >= 0)

    @constraint(model, cDemandBalance[h in H],
        sum(GEN[g, h] for g in G) + NSE[h] == data.demand_by_hour[h] * dt
    )
    @constraint(model, cCapacity[g in G, h in H],
        GEN[g, h] <= CAP[g] * dt
    )

    @objective(model, Min,
        sum(data.fixed_cost[g] * CAP[g] for g in G) +
        sum(data.var_cost[g] * GEN[g, h] for g in G, h in H) +
        sum(nse_cost * NSE[h] for h in H)
    )

    return model, CAP, GEN, NSE
end

function output_path(config::Dict, root::AbstractString, key::AbstractString)
    return resolve_repo_path(root, config["expected_output_files"][key]["path"])
end

function write_outputs(config::Dict, data::ScenarioData, model::Model, CAP, GEN, NSE, root::AbstractString)
    G = data.G
    H = data.H
    nse_cost = Float64(config["parameters"]["non_served_energy_cost"]["value"])

    capacity_rows = DataFrame(
        G = String[],
        Description = String[],
        capacity_mw = Float64[],
        fixed_cost_usd = Float64[],
    )
    for g in G
        capacity_mw = value(CAP[g])
        push!(capacity_rows, (
            g,
            data.description[g],
            capacity_mw,
            capacity_mw * data.fixed_cost[g],
        ))
    end

    generation_rows = DataFrame(Hour = Int[], G = String[], generation_mwh = Float64[])
    for g in G
        for h in H
            push!(generation_rows, (h, g, value(GEN[g, h])))
        end
    end

    nse_rows = DataFrame(Hour = Int[], nse_mwh = Float64[], nse_penalty_usd = Float64[])
    for h in H
        nse_mwh = value(NSE[h])
        push!(nse_rows, (h, nse_mwh, nse_mwh * nse_cost))
    end

    total_fixed_cost = sum(capacity_rows.fixed_cost_usd)
    total_variable_cost = sum(
        data.var_cost[row.G] * row.generation_mwh for row in eachrow(generation_rows)
    )
    total_nse_mwh = sum(nse_rows.nse_mwh)
    total_nse_penalty = sum(nse_rows.nse_penalty_usd)

    summary = Dict(
        "scenario_id" => config["scenario_id"],
        "termination_status" => string(termination_status(model)),
        "primal_status" => string(primal_status(model)),
        "objective_value_usd" => objective_value(model),
        "total_fixed_cost_usd" => total_fixed_cost,
        "total_variable_cost_usd" => total_variable_cost,
        "total_nse_mwh" => total_nse_mwh,
        "total_nse_penalty_usd" => total_nse_penalty,
        "peak_demand_mw" => maximum(Float64.(data.demand.Demand)),
        "hours" => length(H),
        "candidate_generators" => G,
    )

    capacity_path = output_path(config, root, "capacity_build")
    generation_path = output_path(config, root, "hourly_generation")
    nse_path = output_path(config, root, "non_served_energy")
    summary_path = output_path(config, root, "solve_summary")

    mkpath(dirname(capacity_path))
    mkpath(dirname(generation_path))
    mkpath(dirname(nse_path))
    mkpath(dirname(summary_path))

    CSV.write(capacity_path, capacity_rows)
    CSV.write(generation_path, generation_rows)
    CSV.write(nse_path, nse_rows)
    write_json_file(summary_path, summary)

    return summary
end

function validate_solution(config::Dict, data::ScenarioData, CAP, GEN, NSE)
    G = data.G
    H = data.H
    dt = Float64(config["parameters"]["timestep_hours"]["value"])
    tolerance = 1.0e-5

    max_balance_residual = maximum(abs(
        sum(value(GEN[g, h]) for g in G) + value(NSE[h]) - data.demand_by_hour[h] * dt
    ) for h in H)
    max_capacity_violation = maximum(
        value(GEN[g, h]) - value(CAP[g]) * dt for g in G for h in H
    )

    if max_balance_residual > tolerance
        error("Demand balance residual $(max_balance_residual) exceeds tolerance $(tolerance)")
    end
    if max_capacity_violation > tolerance
        error("Capacity violation $(max_capacity_violation) exceeds tolerance $(tolerance)")
    end
end

function main()
    root = repo_root()
    config_path = length(ARGS) >= 1 ? ARGS[1] : joinpath(@__DIR__, "scenario_config.yaml")
    config = load_config(config_path)
    data = read_inputs(config, root)
    model, CAP, GEN, NSE = build_model(config, data, root)

    optimize!(model)

    status = string(termination_status(model))
    accepted = String.(config["solver_settings"]["expected_statuses"])
    if !(status in accepted)
        error("Unexpected solver status $(status); expected one of $(accepted)")
    end

    validate_solution(config, data, CAP, GEN, NSE)
    summary = write_outputs(config, data, model, CAP, GEN, NSE, root)
    println("Solved $(summary["scenario_id"]) with objective $(round(summary["objective_value_usd"]; digits=2)) USD")
end

main()
