"""
Copyright 2013 Steven Diamond

This file is part of CVXPY.

CVXPY is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

CVXPY is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with CVXPY.  If not, see <http://www.gnu.org/licenses/>.
"""

from .. import settings as s
from .. import interface as intf
from ..utilities.ordered_set import OrderedSet
from ..expressions.expression import Expression
from ..expressions.constants import Constant
from ..expressions.variables import Variable
from ..constraints import *
from .objective import Minimize, Maximize
from kktsolver import get_kktsolver

from collections import OrderedDict
import itertools
import numbers
import cvxopt
import cvxopt.solvers
import ecos
# ECHU: ECOS now depends on numpy
import numpy as np
import scipy.sparse as sp

class Problem(object):
    """
    An optimization problem.
    """
    # The solve methods available.
    REGISTERED_SOLVE_METHODS = {}
    # objective - the problem objective.
    # constraints - the problem constraints.
    def __init__(self, objective, constraints=None):
        if constraints is None:
            constraints = []
        self.objective = objective
        self.constraints = constraints
        self.interface = intf.DEFAULT_SPARSE_INTERFACE
        self.dense_interface = intf.DEFAULT_INTERFACE

    # Does the problem satisfy DCP rules?
    def is_dcp(self):
        return all(exp.is_dcp() for exp in self.constraints + [self.objective])

    # Divide the constraints into separate types.
    # Remove duplicate constraint objects.
    def filter_constraints(self, constraints):
        constr_map = {s.EQ: OrderedSet([]), 
                      s.INEQ: OrderedSet([]), 
                      s.SOC: OrderedSet([]), 
                      s.SDP: OrderedSet([]), 
                      s.NONLIN: OrderedSet([])}
        for c in constraints:
            if isinstance(c, EqConstraint):
                constr_map[s.EQ].add(c)
            elif isinstance(c, LeqConstraint):
                constr_map[s.INEQ].add(c)
            elif isinstance(c, SOC):
                constr_map[s.SOC].add(c)
            elif isinstance(c, SDP):
                constr_map[s.SDP].add(c)
            elif isinstance(c, NonlinearConstraint):
                constr_map[s.NONLIN].add(c)
        return constr_map

    # Convert the problem into an affine objective and affine constraints.
    # Also returns the dimensions of the cones for the solver.
    def canonicalize(self):
        constraints = []
        obj, constr = self.objective.canonical_form
        constraints += constr
        unique_constraints = list(set(self.constraints))
        for constr in unique_constraints:
            constraints += constr.canonical_form[1]
        constr_map = self.filter_constraints(constraints)
        dims = {'l': sum(c.size[0]*c.size[1] for c in constr_map[s.INEQ])}
        # Formats SOC and SDP constraints for the solver.
        for constr in itertools.chain(constr_map[s.SOC], constr_map[s.SDP]):
            for ineq_constr in constr.format():
                constr_map[s.INEQ].add(ineq_constr)
        dims['q'] = [c.size[0] for c in constr_map[s.SOC]]
        dims['s'] = [c.size[0] for c in constr_map[s.SDP]]
        return (obj, constr_map, dims)

    # Dispatcher for different solve methods.
    def solve(self, *args, **kwargs):
        func_name = kwargs.pop("method", None)
        if func_name is not None:
            func = Problem.REGISTERED_SOLVE_METHODS[func_name]
            return func(self, *args, **kwargs)
        else:
            return self._solve(*args, **kwargs)

    # Register a solve method.
    @staticmethod
    def register_solve(name, func):
        Problem.REGISTERED_SOLVE_METHODS[name] = func

    # Solves DCP compliant optimization problems.
    # Saves the values of primal and dual variables.
    def _solve(self, solver=s.ECOS, ignore_dcp=False, verbose=False):
        if not self.is_dcp():
            if ignore_dcp:
                print ("Problem does not follow DCP rules. "
                       "Solving a convex relaxation.")
            else:
                raise Exception("Problem does not follow DCP rules.")
        objective,constr_map,dims = self.canonicalize()
        
        all_ineq = itertools.chain(constr_map[s.EQ], constr_map[s.INEQ])
        var_offsets,x_length = self.variables(objective, all_ineq)
       
        c,obj_offset = self.constraints_matrix([objective], var_offsets, x_length,
                                               self.dense_interface, self.dense_interface)
        A,b = self.constraints_matrix(constr_map[s.EQ], var_offsets, x_length,
                                      self.interface, self.dense_interface)
        G,h = self.constraints_matrix(constr_map[s.INEQ], var_offsets, x_length,
                                      self.interface, self.dense_interface)

        # ECHU: get the nonlinear constraints
        F = self.nonlinear_constraint_function(constr_map[s.NONLIN], var_offsets,
                                               x_length)

        # Save original cvxopt solver options.
        old_options = cvxopt.solvers.options
        # Silence cvxopt if verbose is False.
        cvxopt.solvers.options['show_progress'] = verbose
        # Always do one step of iterative refinement after solving KKT system.
        cvxopt.solvers.options['refinement'] = 1
        # Target cvxopt clp if nonlinear constraints exist
        if constr_map[s.NONLIN]:
            # Get custom kktsolver.
            kktsolver = get_kktsolver(G, dims, A, F)
            results = cvxopt.solvers.cpl(c.T,F,G,h,A=A,b=b,
                                         dims=dims,kktsolver=kktsolver)
            status = s.SOLVER_STATUS[s.CVXOPT][results['status']]
            primal_val = results['primal objective']
        # Target cvxopt solver if SDP or invalid for ECOS.
        elif solver == s.CVXOPT or len(dims['s']) > 0 or min(G.size) == 0:
            # Get custom kktsolver.
            kktsolver = get_kktsolver(G, dims, A)
            # Adjust tolerance to account for regularization.
            cvxopt.solvers.options['feastol'] = 2*1e-6
            results = cvxopt.solvers.conelp(c.T,G,h,A=A,b=b,
                                            dims=dims,kktsolver=kktsolver)
            status = s.SOLVER_STATUS[s.CVXOPT][results['status']]
            primal_val = results['primal objective']
        else: # If possible, target ECOS.
            # ECHU: ecos interface has changed and no longer relies on CVXOPT
            # as a result, we have to convert cvxopt data structures into
            # numpy arrays
            #
            # ideally, CVXPY would no longer user CVXOPT, except when calling
            # conelp
            #
            cnp, hnp, bnp = map(lambda x: np.fromiter(iter(x),dtype=np.double,count=len(x)), (c, h, b))
            Gp,Gi,Gx = G.CCS
            m,n1 = G.size
            Ap,Ai,Ax = A.CCS
            p,n2 = A.size
            Gp, Gi, Ap, Ai = map(lambda x: np.fromiter(iter(x),dtype=np.int32,count=len(x)), (Gp,Gi,Ap,Ai))
            Gx, Ax = map(lambda x: np.fromiter(iter(x),dtype=np.double,count=len(x)), (Gx, Ax))
            Gsp = sp.csc_matrix((Gx,Gi,Gp),shape=(m,n1))
            if p == 0:
                Asp = None
                bnp = None
            else:
                Asp = sp.csc_matrix((Ax,Ai,Ap),shape=(p,n2))
                
            # ECHU: end conversion
            results = ecos.solve(cnp,Gsp,hnp,dims,Asp,bnp,verbose=verbose)
            status = s.SOLVER_STATUS[s.ECOS][results['info']['exitFlag']]
            primal_val = results['info']['pcost']

        # Restore original cvxopt solver options.
        cvxopt.solvers.options = old_options

        if status == s.SOLVED:
            self.save_values(results['x'], var_offsets.keys())
            self.save_values(results['y'], constr_map[s.EQ])
            if constr_map[s.NONLIN]:
                self.save_values(results['zl'], constr_map[s.INEQ])
            else:
                self.save_values(results['z'], constr_map[s.INEQ])
            return self.objective._primal_to_result(primal_val - obj_offset[0])
        else:
            return status

    # Returns a map of variable id to horizontal offset
    # and the length of the x vector.
    def variables(self, objective, constraints):
        vars_ = objective.variables()
        for constr in constraints:
            vars_ += constr.variables()
        var_offsets = OrderedDict()
        vert_offset = 0
        for var in set(vars_):
            var_offsets[var] = vert_offset
            vert_offset += var.size[0]*var.size[1]
        return (var_offsets, vert_offset)

    # Saves the values of the optimal primary/dual variables
    # as fields in the variable/constraint objects.
    def save_values(self, result_vec, objects):
        if len(result_vec) > 0:
            # Cast to desired matrix type.
            result_vec = self.dense_interface.const_to_matrix(result_vec)
        offset = 0
        for obj in objects:
            rows,cols = obj.size
            # Handle scalars
            if (rows,cols) == (1,1):
                value = intf.index(result_vec, (offset, 0))
            else:
                value = self.dense_interface.zeros(rows, cols)
                self.dense_interface.block_add(value,
                    result_vec[offset:offset + rows*cols],
                    0, 0, rows, cols)
            obj.save_value(value)
            offset += rows*cols

    # Returns a matrix where each variable coefficient is inserted as a block
    # with upper left corner at matrix[variable offset, constraint offset]
    # and a vector with the constant terms.
    # aff_expressions - a list of affine expressions or constraints.
    # var_offsets - a dict of variable id to horizontal offset.
    # x_length - the length of the x vector.
    # matrix_intf - the matrix interface to use for creating the constraints matrix.
    # vec_intf - the matrix interface to use for creating the constant vector.
    def constraints_matrix(self, aff_expressions, var_offsets, x_length,
                           matrix_intf, vec_intf):
        rows = sum([aff.size[0] * aff.size[1] for aff in aff_expressions])
        cols = x_length
        matrix = matrix_intf.zeros(rows, cols)
        const_vec = vec_intf.zeros(rows, 1)
        vert_offset = 0
        for aff_exp in aff_expressions:
            coefficients = aff_exp.coefficients()
            for var,blocks in coefficients.items():
                # Constant is not in var_offsets.
                for col,block in enumerate(blocks):
                    vert_start = vert_offset + col*aff_exp.size[0]
                    vert_end = vert_start + aff_exp.size[0]
                    if var is s.CONSTANT:
                        const_vec[vert_start:vert_end,:] = block
                    else:
                        horiz_offset = var_offsets[var]
                        horiz_end = horiz_offset + var.size[0]*var.size[1]
                        matrix[vert_start:vert_end, horiz_offset:horiz_end] = block
                        horiz_offset += var.size[1]
            vert_offset += aff_exp.size[0]*aff_exp.size[1]
        return (matrix,-const_vec)

    def nonlinear_constraint_function(self, nl_funcs, var_offsets, x_length):
        """ TODO: ensure that this works with numpy data structs...
        """
        rows = sum([func.size[0] * func.size[1] for func in nl_funcs])
        cols = x_length

        big_x = self.dense_interface.zeros(cols, 1)
        for func in nl_funcs:
            func.place_x0(big_x, var_offsets, self.dense_interface)

        def F(x=None, z=None):
            if x is None: return rows, big_x
            big_f = self.dense_interface.zeros(rows, 1)
            big_Df = self.interface.zeros(rows, cols)
            if z: big_H = self.interface.zeros(cols, cols)

            offset = 0
            for func in nl_funcs:
                local_x = func.extract_variables(x, var_offsets, self.dense_interface)
                if z:
                    f, Df, H = func.f(local_x, z[offset:offset + func.size[0]])
                else:
                    result = func.f(local_x)
                    if result:
                        f, Df = result
                    else:
                        return None
                big_f[offset:offset + func.size[0]] = f
                func.place_Df(big_Df, Df, var_offsets, offset, self.interface)
                if z:
                    func.place_H(big_H, H, var_offsets, self.interface)
                offset += func.size[0]

            if z is None: return big_f, big_Df
            return big_f, big_Df, big_H
        return F

    def __str__(self):
        return repr(self)

    def __repr__(self):
        return "Problem(%s, %s)" % (repr(self.objective), repr(self.constraints))
