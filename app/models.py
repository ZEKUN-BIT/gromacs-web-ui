from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LigandSpec(BaseModel):
    """One ligand of a (possibly multi-ligand) protein-ligand complex."""

    model_config = ConfigDict(extra="forbid")

    key: str = ""
    name: str = ""
    residue: str = ""
    mode: str = "prepared"
    count: int | str = 1
    gro_file: str = ""
    itp_file: str = ""
    structure_file: str = ""
    chemistry_file: str = ""
    smiles: str = ""
    protonation_mode: str = "preserve"
    charge: int | str = 0
    charge_confirmed: bool = False
    ph: float | str = 7.4
    posres: bool = True


class SimulationParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow: str = "protein_md"
    name: str = "GROMACS run"
    dry_run: bool = False
    gmx_bin: str = "gmx"
    force_field: str = "amber19sb"
    water_model: str = "opc"
    solvent_template: str = "auto"
    box_type: str = "dodecahedron"
    box_distance: float = 1.0
    maxwarn: int = 0
    ntmpi: int | None = None
    ntomp: int | None = None
    pin: Literal["auto", "on", "off"] = "auto"
    gpu: bool = False
    verbose: bool = False
    ignh: bool = False
    clean_pdb: bool = False
    pre_nvt: bool = False
    replicas: int = 3
    release_restraints: bool = True
    neutral: bool = True
    positive_ion: str = "NA"
    negative_ion: str = "CL"
    ion_concentration: float | str = ""
    solvent_group: str = "SOL"
    structure_file: str = ""
    protein_file: str = ""
    ligands: list[LigandSpec] = Field(default_factory=list)
    ligand_mode: str = "prepared"
    auto_split_complex_pdb: bool = True
    complex_ligand_residue: str = ""
    ligand_name: str = "lig"
    ligand_count: int = 1
    ligand_structure_file: str = ""
    ligand_chemistry_file: str = ""
    ligand_smiles: str = ""
    ligand_protonation_mode: str = "preserve"
    ligand_gro_file: str = ""
    ligand_itp_file: str = ""
    ligand_charge: int = 0
    ligand_charge_confirmed: bool = False
    ligand_ph: float = 7.4
    charge_method: str = "bcc"
    atom_type: str = "gaff2"
    use_obabel: bool = True
    obabel_bin: str = "obabel"
    acpype_bin: str = "acpype"
    ligand_posres: bool = True
    ligand_heavy_group: str = "System_&_!H*"
    posres_fc_x: str = "1000"
    posres_fc_y: str = "1000"
    posres_fc_z: str = "1000"
    flexible_water: bool = False
    create_index: bool = True
    index_script: str = ""
    custom_stdin: bool = False
    stdin_overrides: str = ""
    mdp_override_enabled: bool = False
    mdp_dt_ps: str = ""
    mdp_nvt_ns: str = ""
    mdp_npt_ns: str = ""
    mdp_md_ns: str = ""
    mdp_temperature_k: str = ""
    mdp_pressure_bar: str = ""
    mdp_tc_grps: str = ""
    mdp_tau_t_ps: str = ""
    mdp_define_equil: str = ""
    mdp_gen_vel: str = ""
    mdp_pcoupl: str = ""
    mdp_constraints: str = ""
    mdp_rcoulomb: str = ""
    mdp_rvdw: str = ""
    mdp_output_interval_ps: str = ""
    protein_group: str = "1"
    ligand_group: str = "13"
    protein_lig_group: str = "20"
    water_ions_group: str = "21"
    tc_groups: str = "Protein_Lig Water_and_Ions"
    production_deffnm: str = "md_0_100"
    run_postprocess_after_md: bool = True
    tpr_file: str = ""
    trajectory_file: str = ""
    index_file: str = ""
    center_group: str = "Protein_Lig"
    output_group: str = "System"
    fit_group: str = "Backbone"
    rmsd_group: str = "Backbone"
    do_center: bool = True
    do_fit: bool = True
    do_dump: bool = True
    keep_centered_trajectory: bool = False
    keep_fitted_trajectory: bool = False
    center_output: str = "md_0_100_center.xtc"
    fit_output: str = "md_0_100_fit.xtc"
    start_pdb: str = "start.pdb"
    pbc_mode: str = "cluster"
    unitcell: str = "compact"
    fit_mode: str = "rot+trans"
    dump_time: str = "0"
    time_unit: str = "ns"
    edr_file: str = "md_0_100.edr"
    do_energy: bool = True
    energy_terms: str = "Potential"
    energy_output: str = "potential.xvg"
    do_rmsd: bool = True
    rmsd_output: str = "rmsd.xvg"
    do_rg: bool = True
    rg_output: str = "gyrate.xvg"
    rg_selection: str = "Protein"
    do_dssp: bool = False
    do_hbond: bool = True
    do_pca: bool = False
    do_sham: bool = False
    hbond_main_a: str = "MainChain+H"
    hbond_main_b: str = "MainChain+H"
    hbond_side_a: str = "SideChain"
    hbond_side_b: str = "SideChain"
    hbond_pw_a: str = "Protein"
    hbond_pw_b: str = "Water"
    pca_align_group: str = "C-alpha"
    pca_group: str = "C-alpha"
    sham_temp: str = "310"
    sham_levels: str = "100"
    files: list[str] = Field(default_factory=list)
    custom_command: str = ""

    @model_validator(mode="after")
    def default_analysis_center_group(self):
        if "center_group" not in self.model_fields_set and self.workflow in {"protein_md", "analysis_rmsd", "analysis_suite"}:
            self.center_group = "Protein"
        return self


class BenchmarkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    threads: list[int] = Field(default_factory=list, max_length=6)
    nsteps: int = Field(default=10_000, ge=1_000, le=500_000)
    gpu: bool = True
    pin: Literal["on", "off"] = "on"


AnalysisFile = Annotated[str, Field(max_length=1024, pattern=r"^[^\x00-\x1f\x7f]*$")]
AnalysisSelection = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f\x7f]+$")]


class ExistingAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", max_length=160)
    tpr_file: AnalysisFile
    trajectory_file: AnalysisFile
    index_file: AnalysisFile = ""
    edr_file: AnalysisFile = ""
    do_rmsd: bool = True
    do_rg: bool = True
    do_energy: bool = False
    do_dssp: bool = False
    do_hbond: bool = False
    do_pca: bool = False
    do_research: bool = False
    research_ligand_group: AnalysisFile = ""
    research_stride: int = Field(default=10, ge=1, le=10000)
    contact_cutoff_nm: float = Field(default=0.45, ge=0.2, le=1.0, allow_inf_nan=False)
    begin_ns: float = Field(default=0, ge=0, allow_inf_nan=False)
    end_ns: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    center_group: AnalysisSelection = "Protein"
    fit_group: AnalysisSelection = "Backbone"
    rmsd_group: AnalysisSelection = "Backbone"
    rg_selection: AnalysisSelection = "Protein"
    energy_terms: AnalysisSelection = "Potential"
    dry_run: bool = False

    @model_validator(mode="after")
    def valid_analysis_window(self):
        if self.end_ns is not None and self.end_ns <= self.begin_ns:
            raise ValueError("分析结束时间必须大于开始时间。")
        if self.research_ligand_group and not self.index_file:
            raise ValueError("配体研究分析需要索引文件和单配体原子组。")
        return self


class ResearchComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference_jobs: list[Annotated[str, Field(min_length=1, max_length=255)]] = Field(min_length=1, max_length=6)
    comparison_jobs: list[Annotated[str, Field(min_length=1, max_length=255)]] = Field(min_length=1, max_length=6)


PreviewRequest = SimulationParams


class ProtocolPreviewRequest(SimulationParams):
    uploaded_mdp: dict[
        Literal["ions.mdp", "minim.mdp", "nvt.mdp", "npt.mdp", "md.mdp"],
        Annotated[str, Field(max_length=262144)],
    ] = Field(default_factory=dict, max_length=5)
