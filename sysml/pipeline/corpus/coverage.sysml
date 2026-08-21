// Every relationship form the parser claims to read, in one file, so that
// `python -m sysml.pipeline.corpus.check` can prove each label in
// `model.RELATIONS` is produced by some syntax rather than only documented.
//
// This is not a model of anything. It is deliberately dense and deliberately
// includes forms the real corpus does not use -- those are exactly the ones that
// rot unnoticed, because nothing fails when they stop working.
package Shared {
    part def SharedThing;
}

package Coverage {
    import Shared::*;
    public alias PartAlias for Coverage::PartA;

    part def PartA;
    part def PartB :> PartA;
    part def PartC specializes PartA;
    port def PortA { out attribute reading : Real; }
    item def ItemA;
    attribute def AttrA;
    flow def FlowA;
    interface def IfaceA { end left : PortA; end right : PortA; }
    connection def ConnA { end one : PartA; end two : PartB; }
    allocation def AllocA;
    metadata def MetaA { attribute why : String; }
    enum def EnumA { enum low; enum high; }
    occurrence def OccA;
    calc def CalcA { in load : Real; return margin : Real; }
    constraint def PositiveLoad { in load : Real; load > 0 }
    concern def SafetyConcern;
    requirement def ReqA { subject item1 : PartA; }
    requirement def ReqB;
    use case def UseA { actor operator : PartA; subject item2 : PartA; }
    view def ViewA;
    viewpoint def VpA;
    rendering def RendA;
    verification def VerA;
    analysis def AnaA;
    action def ActA { in load : Real; }
    state def StateA;

    variation part def Options {
        variant part cheap : PartA;
        variant part fast : PartB;
    }

    part top : PartA {
        // typing, specialisation, redefinition, reference, conjugation
        part alpha : PartB[0..*];
        part beta :> alpha;
        part gamma :>> beta;
        part delta ::> alpha;
        port inbound : PortA;
        port outbound : ~PortA;
        attribute load : Real = 4.0;
        attribute mass = 12 [SI::kg];
        item cargo : ItemA;

        // KerML type operators: none of these is a specialisation
        part exclusive : PartA disjoint from alpha;
        part either : PartA unions alpha, beta;
        part both : PartA intersects alpha, beta;
        part without : PartA differences alpha, beta;
        part reached : PartA chains inbound.reading;
        part opposite : PartA inverse of alpha;

        // connections, interfaces, flows, allocation, binding, dependency
        connect alpha to beta;
        interface link : IfaceA connect inbound to outbound;
        flow cargoFlow of ItemA from inbound to outbound;
        succession flow sequenced from inbound to outbound;
        message ping from alpha to beta via inbound;
        allocate alpha to beta;
        bind load = mass;
        assign load := 9;
        dependency from alpha to beta;

        // behaviour attached to a part
        perform action work : ActA;
        exhibit state machine : StateA;
        event occurrence started;
        snapshot atLaunch;
        timeslice duringFlight;

        // requirements
        satisfy requirement ReqA by alpha;
        verify requirement ReqB by beta;
        refine ReqB from ReqA;
        derive ReqB from ReqA;
        assert constraint { load > 0 }
        assume constraint { mass > 0 }
        require constraint { load < 100 }
        frame concern SafetyConcern;
        include use case UseA;
        stakeholder owner : PartA;
    }

    action def Sequence {
        in load : Real;
        action stepOne : ActA;
        action stepTwo : ActA;
        first stepOne then stepTwo;
        fork split;
        join merged;
        merge joined;
        decide choose;
        terminate finish;
        send ItemA to stepTwo;
        accept delivery : ItemA;
        if load > 0 then stepOne else stepTwo;
        while load > 0 { action retry : ActA; }
        loop { action again : ActA; }
    }

    state def Machine {
        attribute load : Real;
        entry action arrive : ActA;
        do action hold : ActA;
        exit action leave : ActA;
        state idle;
        state busy parallel { state reading; state writing; }
        transition wake first idle accept started if load > 0 do hold then busy;
        transition first busy then idle;
    }

    view def Report {
        expose Coverage::top::**;
        render RendA;
    }

    analysis def Study {
        subject studied : PartA;
        objective { doc /* what the study is for */ }
    }

    #MetaA part tagged : PartA;

    metadata about top {
        why = "so that annotatedBy has a member form too";
    }

    doc /* the package's own documentation */
    comment Note about top /* a comment attached to an element */
    rep Rendered language "html" /* <b>markup</b> */
}
