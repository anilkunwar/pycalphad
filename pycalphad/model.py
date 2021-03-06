"""
The Model module provides support for using a Database to perform
calculations under specified conditions.
"""
from __future__ import division
import copy
from sympy import log, Abs, Add, Mul, Piecewise, Pow, S
from tinydb import where
import pycalphad.variables as v
try:
    set
except NameError:
    from sets import Set as set #pylint: disable=W0622
#pylint: disable=W0142

# What about just running all self._model_*?
class Model(object):
    """
    Models use an abstract representation of the energy function
    for calculation of values under specified conditions.

    Attributes
    ----------
    None yet.

    Methods
    -------
    None yet.

    Examples
    --------
    None yet.
    """
    def __init__(self, dbe, comps, phase):
        self.components = set([x.upper() for x in comps])
        for sublattice in dbe.phases[phase.upper()].constituents:
            if len(set(sublattice).intersection(self.components)) == 0:
                # None of the components in a sublattice are active
                # We cannot build a model of this phase
                raise ValueError(str(sublattice) + \
                    ' has no components in '+str(self.components))
        # Build the abstract syntax tree
        self.ast = self.build_phase(dbe, \
            phase.upper(), dbe.symbols, dbe.search)
        # Need to do one more substitution to catch symbols that are functions
        # of other symbols
        self.ast = self.ast.subs(dbe.symbols)
        self.variables = self.ast.atoms(v.StateVariable)
    def _purity_test(self, constituent_array):
        """
        Check if constituent array only has one species in its array
        This species must also be an active species
        """
        for sublattice in constituent_array:
            if len(sublattice) != 1:
                return False
            if (sublattice[0] not in self.components) and \
                (sublattice[0] != '*'):
                return False
        return True
    def _array_validity(self, constituent_array):
        """
        Check that the current array contains only active species.
        """
        for sublattice in constituent_array:
            valid = set(sublattice).issubset(self.components) \
                or sublattice[0] == '*'
            if not valid:
                return False
        return True
    def _interaction_test(self, constituent_array):
        """
        Check if constituent array has more than one active species in
        its array for at least one sublattice.
        """
        result = False
        for sublattice in constituent_array:
            # check if all elements involved are also active
            valid = set(sublattice).issubset(self.components)
            if len(sublattice) > 1 and valid:
                result = True
            if not valid:
                result = False
                break
        return result
    @staticmethod
    def _Muggianu_correction_dict(comps): #pylint: disable=C0103
        """
        Replace y_i -> y_i + (1 - sum(y involved in parameter)) / m,
        where m is the arity of the interaction parameter.
        Returns a dict converting the list of Symbols (comps) to this.
        m is assumed equal to the length of comps.

        When incorporating binary, ternary or n-ary interaction parameters
        into systems with more than n components, the sum of site fractions
        involved in the interaction parameter may no longer be unity. This
        breaks the symmetry of the parameter. The solution suggested by
        Muggianu, 1975, is to renormalize the site fractions by replacing them
        with a term that will sum to unity even in higher-order systems.
        There are other solutions that involve retaining the asymmetry for
        physical reasons, but this solution works well for components that
        are physically similar.

        This procedure is based on an analysis by Hillert, 1980,
        published in the Calphad journal.
        """
        arity = len(comps)
        return_dict = {}
        correction_term = (S.One - Add(*comps)) / arity #pylint: disable=W0142
        for comp in comps:
            return_dict[comp] = comp + correction_term
        return return_dict
    def build_phase(self, dbe, phase_name, symbols, param_search):
        """
        Apply phase's model hints to build a master SymPy object.
        """
        phase = dbe.phases[phase_name]
        total_energy = S.Zero
        # First, build the reference energy term
        total_energy += self.reference_energy(phase, symbols, param_search)

        # Next, add the ideal mixing term
        total_energy += self.ideal_mixing_energy(phase, symbols, param_search)

        # Next, add the binary, ternary and higher order mixing term
        total_energy += self.excess_mixing_energy(phase, symbols, param_search)

        # Next, we need to handle contributions from magnetic ordering
        total_energy += self.magnetic_energy(phase, symbols, param_search)

        # Next, we handle atomic ordering
        # NOTE: We need to add this one last since it uses the energy
        # as a parameter to figure out the contribution.
        ordered_phase_name = None
        disordered_phase_name = None
        try:
            ordered_phase_name = phase.model_hints['ordered_phase']
            disordered_phase_name = phase.model_hints['disordered_phase']
        except KeyError:
            pass
        if ordered_phase_name == phase_name:
            total_energy += \
                self.atomic_ordering_energy(dbe, disordered_phase_name,
                                            ordered_phase_name,
                                            total_energy,
                                            symbols, param_search)

        return total_energy
    def _redlich_kister_sum(self, phase, symbols, param_type, param_search):
        """
        Construct parameter in Redlich-Kister polynomial basis, using
        the Muggianu ternary parameter extension.
        """
        rk_term = S.Zero
        param_query = (
            (where('phase_name') == phase.name) & \
            (where('parameter_type') == param_type) & \
            (where('constituent_array').test(self._array_validity))
        )
        # search for desired parameters
        params = param_search(param_query)
        for param in params:
            # iterate over every sublattice
            mixing_term = S.One
            for subl_index, comps in enumerate(param['constituent_array']):
                # convert strings to symbols
                if comps[0] == '*':
                    # Handle wildcards in constituent array
                    comp_symbols = \
                        [
                            v.SiteFraction(phase.name, subl_index, comp)
                            for comp in phase.constituents[subl_index]
                        ]
                    mixing_term *= Add(*comp_symbols)
                else:
                    comp_symbols = \
                        [
                            v.SiteFraction(phase.name, subl_index, comp)
                            for comp in comps
                        ]
                    mixing_term *= Mul(*comp_symbols)
                # is this a higher-order interaction parameter?
                if len(comps) == 2 and param['parameter_order'] > 0:
                    # interacting sublattice, add the interaction polynomial
                    redlich_kister_poly = Pow(comp_symbols[0] - \
                        comp_symbols[1], param['parameter_order'])
                    mixing_term *= redlich_kister_poly
                if len(comps) == 3:
                    # 'parameter_order' is an index to a variable when
                    # we are in the ternary interaction parameter case

                    # NOTE: The commercial software packages seem to have
                    # a "feature" where, if only the zeroth
                    # parameter_order term of a ternary parameter is specified,
                    # the other two terms are automatically generated in order
                    # to make the parameter symmetric.
                    # In other words, specifying only this parameter:
                    # PARAMETER G(FCC_A1,AL,CR,NI;0) 298.15  +30300; 6000 N !
                    # Actually implies:
                    # PARAMETER G(FCC_A1,AL,CR,NI;0) 298.15  +30300; 6000 N !
                    # PARAMETER G(FCC_A1,AL,CR,NI;1) 298.15  +30300; 6000 N !
                    # PARAMETER G(FCC_A1,AL,CR,NI;2) 298.15  +30300; 6000 N !
                    #
                    # If either 1 or 2 is specified, no implicit parameters are
                    # generated.
                    # We need to handle this case.
                    if param['parameter_order'] == 0:
                        # are _any_ of the other parameter_orders specified?
                        ternary_param_query = (
                            (where('phase_name') == param['phase_name']) & \
                            (where('parameter_type') == \
                                param['parameter_type']) & \
                            (where('constituent_array') == \
                                param['constituent_array'])
                        )
                        other_tern_params = param_search(ternary_param_query)
                        if len(other_tern_params) == 1 and \
                            other_tern_params[0] == param:
                            # only the current parameter is specified
                            # We need to generate the other two parameters.
                            order_one = copy.deepcopy(param)
                            order_one['parameter_order'] = 1
                            order_two = copy.deepcopy(param)
                            order_two['parameter_order'] = 2
                            # Add these parameters to our iteration.
                            params.extend((order_one, order_two))
                    mixing_term *= comp_symbols[param['parameter_order']]
                    # Perform Muggianu adjustment to site fractions
                    mixing_term = mixing_term.subs(
                        self._Muggianu_correction_dict(comp_symbols)
                    )
            rk_term += mixing_term * \
                param['parameter'].subs(symbols)
        return rk_term
    def reference_energy(self, phase, symbols, param_search):
        """
        Returns the weighted average of the endmember energies
        in symbolic form.
        """
        pure_energy_term = S.Zero
        # Normalize site ratios
        site_ratio_normalization = 0
        for idx, sublattice in enumerate(phase.constituents):
            # sublattices with only vacancies don't count
            if len(sublattice) == 1 and sublattice[0] == 'VA':
                continue
            site_ratio_normalization += phase.sublattices[idx]

        pure_param_query = (
            (where('phase_name') == phase.name) & \
            (where('parameter_order') == 0) & \
            (where('parameter_type') == "G") & \
            (where('constituent_array').test(self._purity_test))
        )

        pure_params = param_search(pure_param_query)

        for param in pure_params:
            site_fraction_product = S.One
            for subl_index, comp in enumerate(param['constituent_array']):
                # We know that comp has one entry, by filtering
                if comp[0] == '*':
                    # Handle wildcards in constituent array
                    comp_symbols = \
                        [
                            v.SiteFraction(phase.name, subl_index, compx)
                            for compx in phase.constituents[subl_index]
                        ]
                    site_fraction_product *= Add(*comp_symbols)
                else:
                    comp_symbol = \
                        v.SiteFraction(phase.name, subl_index, comp[0])
                    site_fraction_product *= comp_symbol
            pure_energy_term += (
                site_fraction_product * param['parameter'].subs(symbols)
            ) / site_ratio_normalization
        return pure_energy_term
    def ideal_mixing_energy(self, phase, symbols, param_search):
        #pylint: disable=W0613
        """
        Returns the ideal mixing energy in symbolic form.
        """
        # Normalize site ratios
        site_ratio_normalization = 0
        site_ratios = phase.sublattices
        for idx, sublattice in enumerate(phase.constituents):
            # sublattices with only vacancies don't count
            if len(sublattice) == 1 and sublattice[0] == 'VA':
                continue
            site_ratio_normalization += site_ratios[idx]

        site_ratios = [c/site_ratio_normalization for c in site_ratios]
        ideal_mixing_term = S.Zero
        for subl_index, sublattice in enumerate(phase.constituents):
            active_comps = set(sublattice).intersection(self.components)
            if len(active_comps) == 1:
                continue # no mixing if only one species in sublattice
            ratio = site_ratios[subl_index]
            for comp in active_comps:
                sitefrac = \
                    v.SiteFraction(phase.name, subl_index, comp)
                mixing_term = Piecewise((sitefrac * log(sitefrac), \
                    Abs(sitefrac) > 1e-16), (0, True))
                ideal_mixing_term += (mixing_term*ratio)
        ideal_mixing_term *= (v.R * v.T)
        return ideal_mixing_term
    def excess_mixing_energy(self, phase, symbols, param_search):
        """
        Build the binary, ternary and higher order interaction term
        Here we use Redlich-Kister polynomial basis by default
        Here we use the Muggianu ternary extension by default
        Replace y_i -> y_i + (1 - sum(y involved in parameter)) / m,
        where m is the arity of the interaction parameter
        """
        excess_mixing_term = S.Zero
        # Normalize site ratios
        site_ratio_normalization = 0
        site_ratios = phase.sublattices
        for idx, sublattice in enumerate(phase.constituents):
            # sublattices with only vacancies don't count
            if len(sublattice) == 1 and sublattice[0] == 'VA':
                continue
            site_ratio_normalization += site_ratios[idx]

        site_ratios = [c/site_ratio_normalization for c in site_ratios]

        interaction_param_query = (
            (where('phase_name') == phase.name) & \
            (
                (where('parameter_type') == "G") | \
                (where('parameter_type') == "L")
            ) & \
            (where('constituent_array').test(self._interaction_test))
        )
        # search for desired parameters
        interaction_params = param_search(interaction_param_query)
        for param in interaction_params:
            # iterate over every sublattice
            mixing_term = S.One
            for subl_index, comps in enumerate(param['constituent_array']):
                # convert strings to symbols
                if comps[0] == '*':
                    # Handle wildcards in constituent array
                    comp_symbols = \
                        [
                            v.SiteFraction(phase.name, subl_index, comp)
                            for comp in phase.constituents[subl_index]
                        ]
                    mixing_term *= Add(*comp_symbols)
                else:
                    comp_symbols = \
                        [
                            v.SiteFraction(phase.name, subl_index, comp)
                            for comp in comps
                        ]
                    mixing_term *= Mul(*comp_symbols)
                # is this a higher-order interaction parameter?
                if len(comps) == 2 and param['parameter_order'] > 0:
                    # interacting sublattice, add the interaction polynomial
                    redlich_kister_poly = Pow(comp_symbols[0] - \
                        comp_symbols[1], param['parameter_order'])
                    mixing_term *= redlich_kister_poly
                if len(comps) == 3:
                    # 'parameter_order' is an index to a variable when
                    # we are in the ternary interaction parameter case

                    # NOTE: The commercial software packages seem to have
                    # a "feature" where, if only the zeroth
                    # parameter_order term of a ternary parameter is specified,
                    # the other two terms are automatically generated in order
                    # to make the parameter symmetric.
                    # In other words, specifying only this parameter:
                    # PARAMETER G(FCC_A1,AL,CR,NI;0) 298.15  +30300; 6000 N !
                    # Actually implies:
                    # PARAMETER G(FCC_A1,AL,CR,NI;0) 298.15  +30300; 6000 N !
                    # PARAMETER G(FCC_A1,AL,CR,NI;1) 298.15  +30300; 6000 N !
                    # PARAMETER G(FCC_A1,AL,CR,NI;2) 298.15  +30300; 6000 N !
                    #
                    # If either 1 or 2 is specified, no implicit parameters are
                    # generated.
                    # We need to handle this case.
                    if param['parameter_order'] == 0:
                        # are _any_ of the other parameter_orders specified?
                        ternary_param_query = (
                            (where('phase_name') == param['phase_name']) & \
                            (where('parameter_type') == \
                                param['parameter_type']) & \
                            (where('constituent_array') == \
                                param['constituent_array'])
                        )
                        other_tern_params = param_search(ternary_param_query)
                        if len(other_tern_params) == 1 and \
                            other_tern_params[0] == param:
                            # only the current parameter is specified
                            # We need to generate the other two parameters.
                            order_one = copy.deepcopy(param)
                            order_one['parameter_order'] = 1
                            order_two = copy.deepcopy(param)
                            order_two['parameter_order'] = 2
                            # Add these parameters to our iteration.
                            interaction_params.extend((order_one, order_two))
                    mixing_term *= comp_symbols[param['parameter_order']]
                    # Perform Muggianu adjustment to site fractions
                    mixing_term = mixing_term.subs(
                        self._Muggianu_correction_dict(comp_symbols)
                    )
                if len(comps) > 3:
                    raise ValueError('Higher-order interactions (n>3) are \
                        not yet supported')
            excess_mixing_term += mixing_term * \
                param['parameter'].subs(symbols) / site_ratio_normalization
        return excess_mixing_term
    def magnetic_energy(self, phase, symbols, param_search):
        #pylint: disable=C0103, R0914
        """
        Return the energy from magnetic ordering in symbolic form.
        The implemented model is the Inden-Hillert-Jarl formulation.
        The approach follows from the background section of W. Xiong, 2011.
        """
        if 'ihj_magnetic_structure_factor' not in phase.model_hints:
            return S.Zero
        if 'ihj_magnetic_afm_factor' not in phase.model_hints:
            return S.Zero
        # Normalize site ratios
        site_ratio_normalization = 0
        for idx, sublattice in enumerate(phase.constituents):
            # sublattices with only vacancies don't count
            if len(sublattice) == 1 and sublattice[0] == 'VA':
                continue
            site_ratio_normalization += phase.sublattices[idx]
        # define basic variables
        afm_factor = phase.model_hints['ihj_magnetic_afm_factor']

        mean_magnetic_moment = \
            self._redlich_kister_sum(phase, symbols, 'BMAGN', param_search)
        beta = Piecewise(
            (mean_magnetic_moment, mean_magnetic_moment > 0),
            (mean_magnetic_moment/afm_factor, mean_magnetic_moment <= 0)
            )

        curie_temp = \
            self._redlich_kister_sum(phase, symbols, 'TC', param_search)
        tc = Piecewise(
            (curie_temp, curie_temp > 0),
            (curie_temp/afm_factor, curie_temp <= 0)
            )
        #print(tc)
        tau = v.T / tc

        # define model parameters
        p = phase.model_hints['ihj_magnetic_structure_factor']
        A = 518/1125 + (11692/15975)*(1/p - 1)
        # factor when tau < 1
        sub_tau = 1 - (1/A) * ((79/(140*p))*(tau**(-1)) + (474/497)*(1/p - 1) \
            * ((tau**3)/6 + (tau**9)/135 + (tau**15)/600)
                              )
        # factor when tau >= 1
        super_tau = -(1/A) * ((tau**-5)/10 + (tau**-15)/315 + (tau**-25)/1500)

        expr_cond_pairs = [(sub_tau, tau < 1),
                           (super_tau, tau >= 1)
                          ]

        g_term = Piecewise(*expr_cond_pairs) #pylint: disable=W0142

        return v.R * v.T * log(beta+1) * \
            g_term / site_ratio_normalization

    @staticmethod
    def mole_fraction(species_name, phase_name, constituent_array,
                      site_ratios):
        """
        Return an abstract syntax tree of the mole fraction of the
        given species as a function of its constituent site fractions.
        """

        # Normalize site ratios
        site_ratio_normalization = 0
        numerator = S.Zero
        for idx, sublattice in enumerate(constituent_array):
            # sublattices with only vacancies don't count
            if len(sublattice) == 1 and sublattice[0] == 'VA':
                continue
            if species_name in list(sublattice):
                site_ratio_normalization += site_ratios[idx]
                numerator += site_ratios[idx] * \
                    v.SiteFraction(phase_name, idx, species_name)

        if site_ratio_normalization == 0 and species_name == 'VA':
            return 1

        if site_ratio_normalization == 0:
            raise ValueError('Couldn\'t find ' + species_name + ' in ' + \
                str(constituent_array))

        return numerator / site_ratio_normalization

    def atomic_ordering_energy(self, dbe, disordered_phase_name,
                               ordered_phase_name, ordered_phase_energy,
                               symbols, param_search):
        """
        Return the atomic ordering contribution in symbolic form.
        Description follows Servant and Ansara, Calphad, 2001.
        """

        # What we need to add here is the energy of
        # the disordered phase, followed by subtracting out the ordered
        # phase energy for the case when all sublattices are equal.
        disordered_term = self.build_phase(dbe, disordered_phase_name,
                                           symbols, param_search)

        # Fix variable names
        variable_rename_dict = {}
        for atom in disordered_term.atoms(v.SiteFraction):
            # Replace disordered phase site fractions with mole fractions of
            # ordered phase site fractions.
            # Special case: Pure vacancy sublattices
            all_species_in_sublattice = \
                dbe.phases[disordered_phase_name].constituents[
                    atom.sublattice_index]
            if atom.species == 'VA' and len(all_species_in_sublattice) == 1:
                # Assume: Pure vacancy sublattices are always last
                vacancy_subl_index = \
                    len(dbe.phases[ordered_phase_name].constituents)-1
                variable_rename_dict[atom] = \
                    v.SiteFraction(
                        ordered_phase_name, vacancy_subl_index, atom.species)
            else:
                # All other cases: replace site fraction with mole fraction
                variable_rename_dict[atom] = \
                    self.mole_fraction(
                        atom.species,
                        ordered_phase_name,
                        dbe.phases[ordered_phase_name].constituents,
                        dbe.phases[ordered_phase_name].sublattices
                        )

        disordered_term = disordered_term.subs(variable_rename_dict)

        # Now handle the ordered term for degenerate sublattice case
        molefraction_dict = {}
        species_dict = {}
        for comp in self.components:
            species_dict[comp] = \
                self.mole_fraction(comp, ordered_phase_name,
                                   dbe.phases[ordered_phase_name].constituents,
                                   dbe.phases[ordered_phase_name].sublattices
                                  )

        # Construct a dictionary that replaces every site fraction with its
        # corresponding mole fraction
        for sitefrac in ordered_phase_energy.atoms(v.SiteFraction):
            molefraction_dict[sitefrac] = species_dict[sitefrac.species]

        subl_equal_term = \
            ordered_phase_energy.subs(molefraction_dict, simultaneous=True)

        return disordered_term - subl_equal_term
