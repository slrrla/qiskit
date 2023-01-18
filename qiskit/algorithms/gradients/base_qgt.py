# This code is part of Qiskit.
#
# (C) Copyright IBM 2022, 2023.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""
Abstract base class of the Quantum Geometric Tensor (QGT).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from copy import copy

import numpy as np

from qiskit.circuit import Parameter, ParameterExpression, QuantumCircuit
from qiskit.primitives import BaseEstimator
from qiskit.primitives.utils import _circuit_key
from qiskit.providers import Options
from qiskit.transpiler.passes import TranslateParameterizedGates

from .. import AlgorithmJob
from .qgt_result import QGTResult
from .utils import (
    DerivativeType,
    GradientCircuit,
    _assign_unique_parameters,
    _make_gradient_parameter_set,
    _make_gradient_parameter_values,
)


class BaseQGT(ABC):
    r"""Base class to computes the Quantum Geometric Tensor (QGT) given a pure,
    parameterized quantum state. QGT is defined as:

    .. math::

        \mathrm{QGT}_{ij}= \langle \partial_i \psi | \partial_j \psi \rangle
            - \langle\partial_i \psi | \psi \rangle \langle\psi | \partial_j \psi \rangle.
    """

    def __init__(
        self,
        estimator: BaseEstimator,
        phase_fix: bool = True,
        derivative_type: DerivativeType = DerivativeType.COMPLEX,
        options: Options | None = None,
    ):
        r"""
        Args:
            estimator: The estimator used to compute the QGT.
            phase_fix: Whether to calculate the second term (phase fix) of the QGT, which is
                :math:`\langle\partial_i \psi | \psi \rangle \langle\psi | \partial_j \psi \rangle`.
                Defaults to ``True``.
            derivative_type: The type of derivative. Can be either ``DerivativeType.REAL``
                ``DerivativeType.IMAG``, or ``DerivativeType.COMPLEX``. Defaults to
                ``DerivativeType.REAL``.

                - ``DerivativeType.REAL`` computes

                .. math::

                    \mathrm{Re(QGT)}_{ij}= \mathrm{Re}[\langle \partial_i \psi | \partial_j \psi \rangle
                        - \langle\partial_i \psi | \psi \rangle \langle\psi | \partial_j \psi \rangle].

                - ``DerivativeType.IMAG`` computes

                .. math::

                    \mathrm{Im(QGT)}_{ij}= \mathrm{Im}[\langle \partial_i \psi | \partial_j \psi \rangle
                        - \langle\partial_i \psi | \psi \rangle \langle\psi | \partial_j \psi \rangle].

                - ``DerivativeType.COMPLEX`` computes

                .. math::

                    \mathrm{QGT}_{ij}= [\langle \partial_i \psi | \partial_j \psi \rangle
                        - \langle\partial_i \psi | \psi \rangle \langle\psi | \partial_j \psi \rangle].

            options: Backend runtime options used for circuit execution. The order of priority is:
                options in ``run`` method > QGT's default options > primitive's default
                setting. Higher priority setting overrides lower priority setting.
        """
        self._estimator: BaseEstimator = estimator
        self._phase_fix: bool = phase_fix
        self._derivative_type: DerivativeType = derivative_type
        self._default_options = Options()
        if options is not None:
            self._default_options.update_options(**options)
        self._qgt_circuit_cache = {}
        self._gradient_circuit_cache: dict[QuantumCircuit, GradientCircuit] = {}

    @property
    def derivative_type(self) -> DerivativeType:
        """The derivative type."""
        return self._derivative_type

    @derivative_type.setter
    def derivative_type(self, derivative_type: DerivativeType) -> None:
        """Set the derivative type."""
        self._derivative_type = derivative_type

    def run(
        self,
        circuits: Sequence[QuantumCircuit],
        parameter_values: Sequence[Sequence[float]],
        parameters: Sequence[Sequence[Parameter] | None] | None = None,
        **options,
    ) -> AlgorithmJob:
        """Run the job of the QGTs on the given circuits.

        Args:
            circuits: The list of quantum circuits to compute the QGTs.
            parameter_values: The list of parameter values to be bound to the circuit.
            parameters: The sequence of parameters to calculate only the QGTs of
                the specified parameters. Each sequence of parameters corresponds to a circuit in
                ``circuits``. Defaults to None, which means that the QGTs of all parameters in
                each circuit are calculated.
            options: Primitive backend runtime options used for circuit execution.
                The order of priority is: options in ``run`` method > QGT's
                default options > primitive's default setting.
                Higher priority setting overrides lower priority setting.

        Returns:
            The job object of the QGTs of the expectation values. The i-th result corresponds to
            ``circuits[i]`` evaluated with parameters bound as ``parameter_values[i]``.

        Raises:
            ValueError: Invalid arguments are given.
        """
        if isinstance(circuits, QuantumCircuit):
            # Allow a single circuit to be passed in.
            circuits = (circuits,)

        if parameters is None:
            # If parameters is None, we calculate the gradients of all parameters in each circuit.
            parameter_sets = [set(circuit.parameters) for circuit in circuits]
        else:
            # If parameters is not None, we calculate the gradients of the specified parameters.
            # None in parameters means that the gradients of all parameters in the corresponding
            # circuit are calculated.
            parameter_sets = [
                set(parameters_) if parameters_ is not None else set(circuits[i].parameters)
                for i, parameters_ in enumerate(parameters)
            ]
        # Validate the arguments.
        self._validate_arguments(circuits, parameter_values, parameter_sets)
        # The priority of run option is as follows:
        # options in ``run`` method > QGT's default options > primitive's default setting.
        opts = copy(self._default_options)
        opts.update_options(**options)
        job = AlgorithmJob(self._run, circuits, parameter_values, parameter_sets, **opts.__dict__)
        job.submit()
        return job

    @abstractmethod
    def _run(
        self,
        circuits: Sequence[QuantumCircuit],
        parameter_values: Sequence[Sequence[float]],
        parameter_sets: Sequence[Sequence[Parameter]],
        **options,
    ) -> QGTResult:
        """Compute the QGTs on the given circuits."""
        raise NotImplementedError()

    def _preprocess(
        self,
        circuits: Sequence[QuantumCircuit],
        parameter_values: Sequence[Sequence[float]],
        parameter_sets: Sequence[set[Parameter]],
        supported_gates: Sequence[str],
    ) -> tuple[Sequence[QuantumCircuit], Sequence[Sequence[float]], Sequence[set[Parameter]]]:
        """Preprocess the gradient. This makes a gradient circuit for each circuit. The gradient
        circuit is a transpiled circuit by using the supported gates, and has unique parameters.
        ``parameter_values`` and ``parameters`` are also updated to match the gradient circuit.

        Args:
            circuits: The list of quantum circuits to compute the gradients.
            parameter_values: The list of parameter values to be bound to the circuit.
            parameter_sets: The sequence of parameters to calculate only the gradients of the specified
                parameters.
            supported_gates: The supported gates used to transpile the circuit.

        Returns:
            The list of gradient circuits, the list of parameter values, and the list of parameters.
            parameter_values and parameters are updated to match the gradient circuit.
        """
        translator = TranslateParameterizedGates(supported_gates)
        g_circuits, g_parameter_values, g_parameter_sets = [], [], []
        for circuit, parameter_value_, parameter_set in zip(
            circuits, parameter_values, parameter_sets
        ):
            circuit_key = _circuit_key(circuit)
            if circuit_key not in self._gradient_circuit_cache:
                unrolled = translator(circuit)
                self._gradient_circuit_cache[circuit_key] = _assign_unique_parameters(unrolled)
            gradient_circuit = self._gradient_circuit_cache[circuit_key]
            g_circuits.append(gradient_circuit.gradient_circuit)
            g_parameter_values.append(
                _make_gradient_parameter_values(circuit, gradient_circuit, parameter_value_)
            )
            g_parameter_sets.append(_make_gradient_parameter_set(gradient_circuit, parameter_set))
        return g_circuits, g_parameter_values, g_parameter_sets

    def _postprocess(
        self,
        results: QGTResult,
        circuits: Sequence[QuantumCircuit],
        parameter_values: Sequence[Sequence[float]],
        parameter_sets: Sequence[set[Parameter]],
    ) -> QGTResult:
        """Postprocess the QGTs. This method computes the QGTs of the original circuits
        by applying the chain rule to the QGTs of the circuits with unique parameters.

        Args:
            results: The computed QGT for the circuits with unique parameters.
            circuits: The list of original circuits submitted for gradient computation.
            parameter_values: The list of parameter values to be bound to the circuits.
            parameter_sets: An optional subset of parameters with respect to which the QGTs should
                be calculated.

        Returns:
            The QGTs of the original circuits.
        """
        qgts, metadata = [], []
        for idx, (circuit, parameter_values_, parameter_set) in enumerate(
            zip(circuits, parameter_values, parameter_sets)
        ):
            dtype = complex if self.derivative_type == DerivativeType.COMPLEX else float
            qgt = np.zeros((len(parameter_set), len(parameter_set)), dtype=dtype)

            gradient_circuit = self._gradient_circuit_cache[_circuit_key(circuit)]
            g_parameter_set = _make_gradient_parameter_set(gradient_circuit, parameter_set)
            # Make a map from the gradient parameter to the respective index in the gradient.
            parameter_indices = [param for param in circuit.parameters if param in parameter_set]
            g_parameter_indices = [
                param
                for param in gradient_circuit.gradient_circuit.parameters
                if param in g_parameter_set
            ]
            g_parameter_indices = {param: i for i, param in enumerate(g_parameter_indices)}

            rows, cols = np.triu_indices(len(parameter_indices))
            for row, col in zip(rows, cols):
                for g_parameter1, coeff1 in gradient_circuit.parameter_map[parameter_indices[row]]:
                    for g_parameter2, coeff2 in gradient_circuit.parameter_map[
                        parameter_indices[col]
                    ]:
                        if isinstance(coeff1, ParameterExpression):
                            local_map = {
                                p: parameter_values_[circuit.parameters.data.index(p)]
                                for p in coeff1.parameters
                            }
                            bound_coeff1 = coeff1.bind(local_map)
                        else:
                            bound_coeff1 = coeff1
                        if isinstance(coeff2, ParameterExpression):
                            local_map = {
                                p: parameter_values_[circuit.parameters.data.index(p)]
                                for p in coeff2.parameters
                            }
                            bound_coeff2 = coeff2.bind(local_map)
                        else:
                            bound_coeff2 = coeff2
                        qgt[row, col] += (
                            float(bound_coeff1)
                            * float(bound_coeff2)
                            * results.qgts[idx][
                                g_parameter_indices[g_parameter1], g_parameter_indices[g_parameter2]
                            ]
                        )
            if self.derivative_type == DerivativeType.IMAG:
                qgt += -1 * np.triu(qgt, k=1).T
            else:
                qgt += np.triu(qgt, k=1).conjugate().T
            qgts.append(qgt)
            metadata.append([{"parameters": parameter_indices}])
        return QGTResult(
            qgts=qgts,
            derivative_type=self.derivative_type,
            metadata=metadata,
            options=results.options,
        )

    def _validate_arguments(
        self,
        circuits: Sequence[QuantumCircuit],
        parameter_values: Sequence[Sequence[float]],
        parameter_sets: Sequence[set[Parameter]],
    ) -> None:
        """Validate the arguments of the ``run`` method.

        Args:
            circuits: The list of quantum circuits to compute the QGTs.
            parameter_values: The list of parameter values to be bound to the circuits.
            parameter_sets: The sequence of parameter sets with respect to which the QGTs should be
                computed. Each set of parameters corresponds to a circuit in ``circuits``.

        Raises:
            ValueError: Invalid arguments are given.
        """
        if len(circuits) != len(parameter_values):
            raise ValueError(
                f"The number of circuits ({len(circuits)}) does not match "
                f"the number of parameter value sets ({len(parameter_values)})."
            )

        if len(circuits) != len(parameter_sets):
            raise ValueError(
                f"The number of circuits ({len(circuits)}) does not match "
                f"the number of the specified parameter sets ({len(parameter_sets)})."
            )

        for i, (circuit, parameter_value) in enumerate(zip(circuits, parameter_values)):
            if not circuit.num_parameters:
                raise ValueError(f"The {i}-th circuit is not parameterised.")
            if len(parameter_value) != circuit.num_parameters:
                raise ValueError(
                    f"The number of values ({len(parameter_value)}) does not match "
                    f"the number of parameters ({circuit.num_parameters}) for the {i}-th circuit."
                )

    @property
    def options(self) -> Options:
        """Return the union of estimator options setting and QGT default options,
        where, if the same field is set in both, the QGT's default options override
        the primitive's default setting.

        Returns:
            The QGT default + estimator options.
        """
        return self._get_local_options(self._default_options.__dict__)

    def update_default_options(self, **options):
        """Update the gradient's default options setting.

        Args:
            **options: The fields to update the default options.
        """

        self._default_options.update_options(**options)

    def _get_local_options(self, options: Options) -> Options:
        """Return the union of the primitive's default setting,
        the QGT default options, and the options in the ``run`` method.
        The order of priority is: options in ``run`` method > QGT's default options > primitive's
        default setting.

        Args:
            options: The fields to update the options

        Returns:
            The QGT default + estimator + run options.
        """
        opts = copy(self._estimator.options)
        opts.update_options(**options)
        return opts